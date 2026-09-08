from __future__ import annotations

import csv
import html
import io
import json
import logging
import math
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import requests
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings


ARM = "https://management.azure.com"
LOG = logging.getLogger("rai-devsub-monitor")


@dataclass(frozen=True)
class MetricRule:
    label: str
    aliases: tuple[tuple[str, ...], ...]
    aggregations: tuple[str, ...] = ()
    interval: str = "P1D"


@dataclass(frozen=True)
class MetricEvidence:
    total: float
    covered_days: int
    expected_days: int
    aggregation: str
    interval: str = "P1D"


@dataclass
class Finding:
    resource_id: str
    resource_group: str
    resource_type: str
    resource_name: str
    cost: float
    currency: str
    classification: str
    evidence: str


RULES: dict[str, MetricRule] = {
    "microsoft.eventhub/namespaces": MetricRule(
        "Event Hubs traffic",
        (("IncomingMessages",), ("OutgoingMessages",), ("IncomingBytes",), ("OutgoingBytes",)),
    ),
    "microsoft.compute/virtualmachines": MetricRule(
        "VM compute/network",
        (("Percentage CPU", "PercentageCPU"), ("Network In Total", "NetworkInTotal"), ("Network Out Total", "NetworkOutTotal")),
        ("Average", "Total", "Total"),
    ),
    "microsoft.containerregistry/registries": MetricRule(
        "ACR push/pull",
        (("SuccessfulPushCount", "PushCount"), ("SuccessfulPullCount", "PullCount")),
    ),
    "microsoft.search/searchservices": MetricRule(
        "Search query/indexing",
        (("SearchQueriesPerSecond", "SearchQueries"), ("DocumentsProcessedCount", "IndexingDocuments")),
        ("Average", "Total"),
    ),
    "microsoft.kusto/clusters": MetricRule(
        "ADX query/ingestion",
        (("QueryResult",), ("IngestionVolumeInMB", "IngestionResult", "IngestionCount")),
        ("Count", "Total"),
    ),
    "microsoft.containerinstance/containergroups": MetricRule(
        "ACI compute/network",
        (("CpuUsage",), ("NetworkBytesReceivedPerSecond",), ("NetworkBytesTransmittedPerSecond",)),
        ("Average", "Average", "Average"),
        "PT1H",
    ),
}


def retry_delay(headers: Any, fallback: float) -> float:
    delays: list[float] = []
    for name, value in headers.items():
        normalized = name.lower()
        if normalized != "retry-after" and not (
            normalized.startswith("x-ms-ratelimit-") and normalized.endswith("retry-after")
        ):
            continue
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            delays.append(delay)
    return max(delays, default=fallback) + random.uniform(0, 5)


class ArmClient:
    def __init__(self, credential: DefaultAzureCredential) -> None:
        self.credential = credential
        self.session = requests.Session()

    def request(self, method: str, url: str, *, retry_budget: float = 180, **kwargs: Any) -> requests.Response:
        if url.startswith("/"):
            url = ARM + url
        deadline = time.monotonic() + retry_budget
        custom_headers = kwargs.pop("headers", {})
        cost_query = "/microsoft.costmanagement/" in url.lower()
        for attempt in range(12):
            token = self.credential.get_token("https://management.azure.com/.default").token
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            headers.update(custom_headers)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("ARM retry time budget exhausted")
            try:
                response = self.session.request(method, url, headers=headers, timeout=min(120, remaining), **kwargs)
                if response.status_code not in (429, 500, 502, 503, 504):
                    response.raise_for_status()
                    return response
                retry_headers = response.headers
                reason = f"HTTP {response.status_code}"
                response.close()
            except (requests.Timeout, requests.ConnectionError) as exc:
                retry_headers = {}
                reason = type(exc).__name__
            if attempt == 11:
                raise RuntimeError(f"ARM request failed after 12 attempts: {reason}")
            fallback = min(30 * 2**attempt, 300) if cost_query else min(2**attempt, 60)
            wait = retry_delay(retry_headers, fallback)
            if wait >= deadline - time.monotonic():
                raise RuntimeError(f"ARM retry time budget exhausted: {reason}")
            LOG.warning("Transient ARM failure %s; retrying in %.1fs (attempt %d/12)", reason, wait, attempt + 1)
            time.sleep(wait)
        raise RuntimeError("ARM retry attempts exhausted")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def cost_rows(client: ArmClient, subscription_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    url = f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query?api-version=2023-03-01"
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start.isoformat(), "to": (end - timedelta(seconds=1)).isoformat()},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "ResourceType"},
                {"type": "Dimension", "name": "ResourceGroupName"},
            ],
        },
    }
    rows: list[dict[str, Any]] = []
    visited: set[str] = set()
    while url:
        if url in visited:
            raise RuntimeError("Cost Management returned a repeated pagination URL")
        visited.add(url)
        result = client.request("POST", url, json=body, retry_budget=1200).json()["properties"]
        columns = [column["name"] for column in result["columns"]]
        rows.extend(dict(zip(columns, row, strict=True)) for row in result.get("rows", []))
        url = result.get("nextLink")
        if url and not url.startswith(f"{ARM}/"):
            raise RuntimeError("Cost Management returned a non-ARM pagination URL")
    return rows


def metric_definitions(client: ArmClient, resource_id: str) -> dict[str, str]:
    path = quote(resource_id, safe="/")
    url = f"{ARM}{path}/providers/microsoft.insights/metricDefinitions?api-version=2018-01-01"
    values = client.request("GET", url).json().get("value", [])
    definitions: dict[str, str] = {}
    for item in values:
        name = item.get("name", {})
        canonical = name.get("value")
        if canonical:
            definitions[canonical.casefold()] = canonical
            if name.get("localizedValue"):
                definitions[name["localizedValue"].casefold()] = canonical
    return definitions


def resolve_metrics(definitions: dict[str, str], rule: MetricRule) -> list[str] | None:
    selected: list[str] = []
    for alias_group in rule.aliases:
        match = next((definitions[a.casefold()] for a in alias_group if a.casefold() in definitions), None)
        if not match:
            return None
        selected.append(match)
    return list(dict.fromkeys(selected))


def metric_totals(
    client: ArmClient, resource_id: str, metrics: list[str], start: datetime, end: datetime,
    aggregations: dict[str, str] | None = None,
    interval: str = "P1D",
) -> dict[str, MetricEvidence]:
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start or any(value.time() != datetime.min.time() for value in (start, end)):
        raise ValueError("Metrics require a nonempty window of complete UTC days")
    expected_days = {start + timedelta(days=offset) for offset in range((end - start).days)}
    step = {"P1D": timedelta(days=1), "PT1H": timedelta(hours=1)}[interval]
    points_per_day = timedelta(days=1) // step
    expected_points = {day + step * offset for day in expected_days for offset in range(points_per_day)}
    path = quote(resource_id, safe="/")
    start_utc = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "api-version": "2018-01-01",
        "timespan": f"{start_utc}/{end_utc}",
        "interval": interval,
        "autoAdjustTimegrain": "false",
        "validateDimensions": "false",
    }
    grouped: dict[str, list[str]] = {}
    for name in metrics:
        aggregation = (aggregations or {}).get(name, "Total")
        grouped.setdefault(aggregation, []).append(name)
    totals: dict[str, MetricEvidence] = {}
    for aggregation, names in grouped.items():
        response = client.request(
            "GET", f"{ARM}{path}/providers/microsoft.insights/metrics",
            params={**params, "metricnames": ",".join(names), "aggregation": aggregation},
        ).json()
        if response.get("interval") != interval:
            raise ValueError("Metrics did not return the requested interval")
        for metric in response.get("value", []):
            name = metric.get("name", {}).get("value")
            if name not in names or metric.get("errorCode", "Success") != "Success":
                continue
            observed = 0.0
            series_coverage: list[set[datetime]] = []
            for series in metric.get("timeseries", []):
                covered: set[datetime] = set()
                for point in series.get("data", []):
                    value = point.get(aggregation.lower())
                    if value is None:
                        continue
                    timestamp = datetime.fromisoformat(point["timeStamp"])
                    if timestamp.utcoffset() is None or timestamp not in expected_points:
                        continue
                    numeric = float(value)
                    if not math.isfinite(numeric):
                        raise ValueError(f"Non-finite metric value for {name}")
                    observed += abs(numeric)
                    covered.add(timestamp)
                series_coverage.append(covered)
            common_points = set.intersection(*series_coverage) if series_coverage else set()
            covered_days = sum(
                all(day + step * offset in common_points for offset in range(points_per_day))
                for day in expected_days
            )
            totals[name] = MetricEvidence(observed, covered_days, len(expected_days), aggregation, interval)
    return totals


def classify(client: ArmClient, resource_id: str, resource_type: str, start: datetime, end: datetime) -> tuple[str, str]:
    rule = RULES.get(resource_type.casefold())
    if not rule:
        return "Unknown", "No resource-specific idle rule in MVP"
    try:
        selected = resolve_metrics(metric_definitions(client, resource_id), rule)
        if not selected:
            return "Unknown", f"Required {rule.label} metrics are not all exposed"
        aggregations = dict(zip(selected, rule.aggregations or ("Total",) * len(selected), strict=True))
        totals = metric_totals(client, resource_id, selected, start, end, aggregations, rule.interval)
        missing = [name for name in selected if name not in totals]
        evidence = "; ".join(
            f"{name}: sum({item.interval} {item.aggregation})={item.total:.3f}, coverage={item.covered_days}/{item.expected_days} days"
            for name, item in totals.items()
        )
        if missing:
            evidence += "; Missing metrics: " + ", ".join(missing)
        if any(item.total > 0 for item in totals.values()):
            return "Active", evidence
        if missing or any(item.covered_days != item.expected_days for item in totals.values()):
            return "Unknown", "Incomplete daily coverage. " + evidence
        return "Idle candidate", evidence
    except Exception as exc:  # continue the report while preserving uncertainty
        LOG.warning("Metric classification failed for %s: %s", resource_id, exc)
        return "Unknown", f"Metric query failed: {type(exc).__name__}: {exc}"


def resource_name(resource_id: str) -> str:
    return resource_id.rstrip("/").split("/")[-1]


def render_csv(findings: list[Finding]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(asdict(findings[0]).keys()) if findings else list(Finding.__annotations__))
    writer.writeheader()
    writer.writerows(asdict(finding) for finding in findings)
    return output.getvalue()


def render_html(findings: list[Finding], start: datetime, end: datetime, threshold: float) -> str:
    idle_findings = [item for item in findings if item.classification == "Idle candidate" and item.cost > threshold]
    unknown_findings = [item for item in findings if item.classification == "Unknown" and item.cost > threshold]
    sections: list[str] = []
    for title, items in (("Idle candidates", idle_findings), ("Unknown: review required", unknown_findings)):
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(item.classification)}</td><td>{html.escape(item.currency)} {item.cost:,.2f}</td>"
            f'<td><a href="https://portal.azure.com/#resource{quote(item.resource_id, safe="/")}">{html.escape(item.resource_name)}</a></td>'
            f"<td>{html.escape(item.resource_type)}</td><td>{html.escape(item.resource_group)}</td>"
            f"<td>{html.escape(item.evidence)}</td></tr>"
            for item in sorted(items, key=lambda item: item.cost, reverse=True)
        )
        if not rows:
            rows = '<tr><td colspan="6">None.</td></tr>'
        sections.append(f"""<h3>{title}: {len(items)}</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<thead><tr><th>Status</th><th>30d cost</th><th>Resource</th><th>Type</th><th>Resource group</th><th>Evidence</th></tr></thead>
<tbody>{rows}</tbody></table>""")
    return f"""<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif">
<h2>RAI Dev subscription cost and idle-resource report</h2>
<p>Window: {start.date()} (inclusive) to {end.date()} (exclusive), UTC. Threshold: &gt; USD {threshold:,.2f}.</p>
<p><strong>Read-only advisory:</strong> Review dependencies, retained data and ownership before any action. Unknown means insufficient evidence, not idle or safe to delete. Active resources are omitted.</p>
{"".join(sections)}</body></html>"""


def send_report(trigger: str, payload: dict[str, Any]) -> None:
    try:
        response = requests.post(trigger, json=payload, timeout=180)
    except requests.RequestException:
        raise RuntimeError("Email confirmation unavailable; check the Logic App run before retrying") from None
    if response.status_code != 200:
        raise RuntimeError(f"Email was not confirmed by the Logic App (HTTP {response.status_code})")
    try:
        acknowledgement = response.json()
    except ValueError:
        raise RuntimeError("Logic App returned an invalid email acknowledgement") from None
    if not isinstance(acknowledgement, dict) or acknowledgement.get("status") != "Sent" or acknowledgement.get("runId") != payload["runId"]:
        raise RuntimeError("Logic App did not acknowledge this report's email")


def upload_reports(
    credential: DefaultAzureCredential, account: str, container: str, prefix: str, payloads: dict[str, tuple[str, str]]
) -> dict[str, str]:
    service = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential)
    urls: dict[str, str] = {}
    for extension, (content, content_type) in payloads.items():
        name = f"{prefix}/report.{extension}"
        blob = service.get_blob_client(container, name)
        blob.upload_blob(content.encode("utf-8"), overwrite=True, content_settings=ContentSettings(content_type=content_type))
        urls[extension] = blob.url
    return urls


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    subscription_id = env("SUBSCRIPTION_ID")
    threshold = float(os.getenv("COST_THRESHOLD_USD", "100"))
    recipient = env("EMAIL_TO")
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30)
    run_id = now.strftime("%Y/%m/%d/%Y%m%dT%H%M%SZ")

    credential = DefaultAzureCredential(managed_identity_client_id=os.getenv("AZURE_CLIENT_ID"))
    client = ArmClient(credential)
    rows = cost_rows(client, subscription_id, start, end)
    findings: list[Finding] = []
    candidates = [row for row in rows if float(row.get("Cost", 0) or 0) > threshold and str(row.get("ResourceId", "") or "").startswith("/subscriptions/")]
    LOG.info("Cost query returned %d rows; classifying %d resources above threshold", len(rows), len(candidates))
    for row in candidates:
        cost = float(row.get("Cost", 0) or 0)
        resource_id = str(row.get("ResourceId", "") or "")
        resource_type = str(row.get("ResourceType", "") or "")
        classification, evidence = classify(client, resource_id, resource_type, start, end)
        findings.append(
            Finding(
                resource_id=resource_id,
                resource_group=str(row.get("ResourceGroupName", "") or ""),
                resource_type=resource_type,
                resource_name=resource_name(resource_id),
                cost=cost,
                currency=str(row.get("Currency", "USD") or "USD"),
                classification=classification,
                evidence=evidence,
            )
        )
    findings.sort(key=lambda item: item.cost, reverse=True)

    report_html = render_html(findings, start, end, threshold)
    report_json = json.dumps(
        {"runId": run_id, "start": start.isoformat(), "end": end.isoformat(), "findings": [asdict(x) for x in findings]},
        indent=2,
    )
    urls = upload_reports(
        credential,
        env("STORAGE_ACCOUNT"),
        os.getenv("REPORT_CONTAINER", "reports"),
        run_id,
        {
            "json": (report_json, "application/json"),
            "csv": (render_csv(findings), "text/csv"),
            "html": (report_html, "text/html"),
        },
    )
    trigger = env("LOGIC_APP_TRIGGER_URL")
    send_report(
        trigger,
        {
            "to": recipient,
            "subject": (
                f"RAI Dev cost monitor: {sum(item.classification == 'Idle candidate' for item in findings)} idle candidates, "
                f"{sum(item.classification == 'Unknown' for item in findings)} unknown"
            ),
            "html": report_html,
            "runId": run_id,
            "reportUrls": urls,
        },
    )
    LOG.info("Completed run %s: %d resources above threshold; reports=%s", run_id, len(findings), urls)


if __name__ == "__main__":
    main()