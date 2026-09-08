from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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
    ),
    "microsoft.containerregistry/registries": MetricRule(
        "ACR push/pull",
        (("SuccessfulPushCount", "PushCount"), ("SuccessfulPullCount", "PullCount")),
    ),
    "microsoft.search/searchservices": MetricRule(
        "Search query/indexing",
        (("SearchQueriesPerSecond", "SearchQueries"), ("DocumentsProcessedCount", "IndexingDocuments")),
    ),
    "microsoft.kusto/clusters": MetricRule(
        "ADX query/ingestion",
        (("QueryCount", "Queries"), ("IngestionVolumeInMB", "IngestionResult", "IngestionCount")),
    ),
    "microsoft.containerinstance/containergroups": MetricRule(
        "ACI compute/network",
        (("CpuUsage",), ("NetworkBytesReceivedPerSecond",), ("NetworkBytesTransmittedPerSecond",)),
    ),
}


class ArmClient:
    def __init__(self, credential: DefaultAzureCredential) -> None:
        self.credential = credential
        self.session = requests.Session()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if url.startswith("/"):
            url = ARM + url
        for attempt in range(10):
            token = self.credential.get_token("https://management.azure.com/.default").token
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            headers.update(kwargs.pop("headers", {}))
            response = self.session.request(method, url, headers=headers, timeout=120, **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                response.raise_for_status()
                return response
            wait = int(response.headers.get("Retry-After", min(2**attempt, 60)))
            LOG.warning("Transient ARM response %s; retrying in %ss", response.status_code, wait)
            time.sleep(wait)
        response.raise_for_status()
        return response


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
        "timePeriod": {"from": start.isoformat(), "to": end.isoformat()},
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
    result = client.request("POST", url, json=body).json()["properties"]
    columns = [column["name"] for column in result["columns"]]
    return [dict(zip(columns, row, strict=True)) for row in result.get("rows", [])]


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
    client: ArmClient, resource_id: str, metrics: list[str], start: datetime, end: datetime
) -> dict[str, float]:
    path = quote(resource_id, safe="/")
    start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "api-version": "2018-01-01",
        "metricnames": ",".join(metrics),
        "timespan": f"{start_utc}/{end_utc}",
        "interval": "P1D",
        "aggregation": "Total,Average,Count",
        "autoAdjustTimegrain": "true",
        "validateDimensions": "false",
    }
    values = client.request("GET", f"{ARM}{path}/providers/microsoft.insights/metrics", params=params).json().get("value", [])
    totals: dict[str, float] = {}
    for metric in values:
        name = metric.get("name", {}).get("value", "unknown")
        observed = 0.0
        samples = 0
        for series in metric.get("timeseries", []):
            for point in series.get("data", []):
                if point.get("total") is not None:
                    observed += abs(float(point["total"]))
                    samples += 1
                elif point.get("average") is not None:
                    observed += abs(float(point["average"]))
                    samples += 1
                elif point.get("count") is not None:
                    observed += abs(float(point["count"]))
                    samples += 1
        if samples:
            totals[name] = observed
    return totals


def classify(client: ArmClient, resource_id: str, resource_type: str, start: datetime, end: datetime) -> tuple[str, str]:
    rule = RULES.get(resource_type.casefold())
    if not rule:
        return "Unknown", "No resource-specific idle rule in MVP"
    try:
        selected = resolve_metrics(metric_definitions(client, resource_id), rule)
        if not selected:
            return "Unknown", f"Required {rule.label} metrics are not all exposed"
        totals = metric_totals(client, resource_id, selected, start, end)
        missing = [name for name in selected if name not in totals]
        if missing:
            return "Unknown", "No datapoints for required metrics: " + ", ".join(missing)
        evidence = "; ".join(f"{name}={totals[name]:.3f}" for name in selected)
        return ("Idle candidate" if all(totals[name] == 0 for name in selected) else "Active", evidence)
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
    idle_findings = [item for item in findings if item.classification == "Idle candidate"]
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.classification)}</td><td>${item.cost:,.2f}</td>"
        f"<td>{html.escape(item.resource_name)}</td><td>{html.escape(item.resource_type)}</td>"
        f"<td>{html.escape(item.resource_group)}</td><td>{html.escape(item.evidence)}</td>"
        "</tr>"
        for item in idle_findings
    )
    if not rows:
        rows = '<tr><td colspan="6">No idle candidates found.</td></tr>'
    return f"""<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif">
<h2>RAI Dev subscription cost and idle-resource report</h2>
<p>Window: {start.date()} through {end.date()} UTC. Threshold: &gt; ${threshold:,.2f}. Idle candidates: {len(idle_findings)}.</p>
<p><strong>Read-only advisory:</strong> This email includes idle candidates only. Active and unknown resources are omitted. Review dependencies and ownership before any action.</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<thead><tr><th>Status</th><th>30d cost</th><th>Resource</th><th>Type</th><th>Resource group</th><th>Evidence</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""


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
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    run_id = end.strftime("%Y/%m/%d/%Y%m%dT%H%M%SZ")

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
    response = requests.post(
        trigger,
        json={
            "to": recipient,
            "subject": f"RAI Dev cost monitor: {sum(x.classification == 'Idle candidate' for x in findings)} idle candidates",
            "html": report_html,
            "runId": run_id,
            "reportUrls": urls,
        },
        timeout=120,
    )
    response.raise_for_status()
    LOG.info("Completed run %s: %d resources above threshold; reports=%s", run_id, len(findings), urls)


if __name__ == "__main__":
    main()