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
    activity_kind: str = "Data-plane activity"


@dataclass(frozen=True)
class MetricEvidence:
    total: float
    covered_days: int
    expected_days: int
    aggregation: str
    interval: str = "P1D"
    last_nonzero_at: datetime | None = None


@dataclass(frozen=True)
class ActivityObservation:
    last_observed_at: str = "Unavailable"
    signal: str = "Unavailable"
    evidence: str = "No resource-specific usage rule"
    covered_days: int = 0
    expected_days: int = 0


@dataclass(frozen=True)
class Assessment:
    classification: str
    evidence: str
    idle_likelihood: str
    likelihood_basis: str
    activity: ActivityObservation | None = None
    metrics: tuple[str, ...] = ()


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
    idle_likelihood: str = "Not assessed"
    likelihood_basis: str = ""
    last_observed_activity_at: str = "Unavailable"
    activity_signal: str = "Unavailable"
    activity_observation: str = "No resource-specific usage rule"


RULES: dict[str, MetricRule] = {
    "microsoft.eventhub/namespaces": MetricRule(
        "Event Hubs traffic",
        (("IncomingMessages",), ("OutgoingMessages",), ("IncomingBytes",), ("OutgoingBytes",)),
    ),
    "microsoft.compute/virtualmachines": MetricRule(
        "VM compute/network",
        (("Percentage CPU", "PercentageCPU"), ("Network In Total", "NetworkInTotal"), ("Network Out Total", "NetworkOutTotal")),
        ("Average", "Total", "Total"),
        activity_kind="Workload activity proxy",
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
        "Workload activity proxy",
    ),
}


ACTIVITY_RULES: dict[str, MetricRule] = {
    "microsoft.cache/redis": MetricRule(
        "Redis commands",
        (("alltotalcommandsprocessed", "totalcommandsprocessed"),),
        ("Total",),
    ),
    "microsoft.cdn/profiles": MetricRule(
        "CDN requests",
        (("RequestCount",),),
        ("Total",),
    ),
    "microsoft.cognitiveservices/accounts": MetricRule(
        "Cognitive Services calls",
        (("TotalCalls", "TotalTransactions", "SuccessfulCalls"),),
        ("Total",),
    ),
    "microsoft.compute/virtualmachinescalesets": MetricRule(
        "VMSS compute/network",
        (
            ("Percentage CPU", "PercentageCPU"),
            ("Network In Total", "Network In"),
            ("Network Out Total", "Network Out"),
        ),
        ("Average", "Total", "Total"),
        activity_kind="Workload activity proxy",
    ),
    "microsoft.dashboard/grafana": MetricRule(
        "Grafana HTTP requests",
        (("HttpRequestCount",),),
        ("Count",),
    ),
    "microsoft.datafactory/factories": MetricRule(
        "Data Factory pipeline runs",
        (("PipelineSucceededRuns",), ("PipelineFailedRuns",), ("PipelineCancelledRuns",)),
        ("Total", "Total", "Total"),
        activity_kind="Pipeline workload activity",
    ),
    "microsoft.documentdb/databaseaccounts": MetricRule(
        "Cosmos DB requests",
        (("TotalRequests", "TotalRequestsPreview"),),
        ("Count",),
    ),
    "microsoft.keyvault/managedhsms": MetricRule(
        "Managed HSM API calls",
        (("ServiceApiHit",),),
        ("Count",),
    ),
    "microsoft.machinelearningservices/workspaces": MetricRule(
        "Azure Machine Learning runs",
        (("Runs",),),
        ("Total",),
        activity_kind="Workspace workload activity",
    ),
    "microsoft.network/azurefirewalls": MetricRule(
        "Azure Firewall data processed",
        (("DataProcessed",),),
        ("Total",),
    ),
    "microsoft.servicebus/namespaces": MetricRule(
        "Service Bus messages",
        (("IncomingMessages",), ("OutgoingMessages",)),
        ("Total", "Total"),
    ),
    "microsoft.storage/storageaccounts": MetricRule(
        "Storage transactions",
        (("Transactions",),),
        ("Total",),
    ),
    "microsoft.web/serverfarms": MetricRule(
        "App Service plan network",
        (("BytesReceived",), ("BytesSent",)),
        ("Total", "Total"),
        activity_kind="Workload activity proxy",
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
            last_nonzero_at: datetime | None = None
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
                    if numeric != 0 and (last_nonzero_at is None or timestamp > last_nonzero_at):
                        last_nonzero_at = timestamp
                    covered.add(timestamp)
                series_coverage.append(covered)
            common_points = set.intersection(*series_coverage) if series_coverage else set()
            covered_days = sum(
                all(day + step * offset in common_points for offset in range(points_per_day))
                for day in expected_days
            )
            totals[name] = MetricEvidence(
                observed, covered_days, len(expected_days), aggregation, interval, last_nonzero_at
            )
    return totals


def summarize_activity(
    rule: MetricRule, metrics: tuple[str, ...], totals: dict[str, MetricEvidence], expected_days: int
) -> ActivityObservation:
    missing = [name for name in metrics if name not in totals]
    covered_days = min((totals[name].covered_days if name in totals else 0 for name in metrics), default=0)
    last_nonzero_at = max(
        (item.last_nonzero_at for item in totals.values() if item.last_nonzero_at is not None),
        default=None,
    )
    precision = "hour" if rule.interval == "PT1H" else "day"
    signal = f"{rule.activity_kind} via Azure Monitor metrics: {', '.join(metrics)}"
    coverage = f"minimum daily coverage={covered_days}/{expected_days} days"
    if last_nonzero_at is not None:
        timestamp = last_nonzero_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return ActivityObservation(
            timestamp,
            signal,
            f"Latest observed nonzero {precision} bucket in the {expected_days}-day window; {coverage}",
            covered_days,
            expected_days,
        )
    if not missing and covered_days == expected_days:
        return ActivityObservation(
            f"Not observed in last {expected_days} days",
            signal,
            f"No nonzero {precision} bucket observed in {expected_days} complete days; full daily coverage",
            covered_days,
            expected_days,
        )
    return ActivityObservation(
        "Unavailable",
        signal,
        f"No nonzero bucket observed, but {coverage}; missing evidence cannot prove inactivity",
        covered_days,
        expected_days,
    )


def assess(client: ArmClient, resource_id: str, resource_type: str, start: datetime, end: datetime) -> Assessment:
    rule = RULES.get(resource_type.casefold())
    if not rule:
        return Assessment(
            "Unknown",
            "No resource-specific idle rule in MVP",
            "Not assessed",
            "No resource-specific usage evidence is available",
            ActivityObservation(),
        )
    try:
        selected = resolve_metrics(metric_definitions(client, resource_id), rule)
        if not selected:
            return Assessment(
                "Unknown",
                f"Required {rule.label} metrics are not all exposed",
                "Not assessed",
                "Required usage evidence is unavailable",
                ActivityObservation(
                    "Unavailable",
                    f"{rule.activity_kind}: {rule.label}",
                    "Required activity metrics are not all exposed",
                ),
            )
        aggregations = dict(zip(selected, rule.aggregations or ("Total",) * len(selected), strict=True))
        totals = metric_totals(client, resource_id, selected, start, end, aggregations, rule.interval)
        metrics = tuple(selected)
        activity = summarize_activity(rule, metrics, totals, (end - start).days)
        missing = [name for name in selected if name not in totals]
        evidence = "; ".join(
            f"{name}: sum({item.interval} {item.aggregation})={item.total:.3f}, coverage={item.covered_days}/{item.expected_days} days"
            for name, item in totals.items()
        )
        if missing:
            evidence += "; Missing metrics: " + ", ".join(missing)
        if any(item.total > 0 for item in totals.values()):
            return Assessment(
                "Active", evidence, "Low", "At least one required usage metric is nonzero", activity, metrics
            )
        if missing or any(item.covered_days != item.expected_days for item in totals.values()):
            coverage = min(
                (
                    totals[name].covered_days / totals[name].expected_days
                    if name in totals and totals[name].expected_days else 0
                )
                for name in selected
            )
            if coverage >= 0.9:
                likelihood = "High"
            elif coverage >= 0.5:
                likelihood = "Medium"
            elif coverage > 0:
                likelihood = "Low"
            else:
                likelihood = "Not assessed"
            return Assessment(
                "Unknown",
                "Incomplete daily coverage. " + evidence,
                likelihood,
                f"Observed usage is zero with {coverage:.0%} minimum required-metric coverage; 100% is required for idle",
                activity,
                metrics,
            )
        return Assessment(
            "Idle candidate",
            evidence,
            "High",
            "All required usage metrics are zero with full coverage",
            activity,
            metrics,
        )
    except Exception as exc:  # continue the report while preserving uncertainty
        LOG.warning("Metric classification failed for %s: %s", resource_id, exc)
        return Assessment(
            "Unknown",
            f"Metric query failed: {type(exc).__name__}: {exc}",
            "Not assessed",
            "Usage evidence could not be queried",
            ActivityObservation("Unavailable", f"{rule.activity_kind}: {rule.label}", "Usage metrics could not be queried"),
        )


def classify(client: ArmClient, resource_id: str, resource_type: str, start: datetime, end: datetime) -> tuple[str, str]:
    result = assess(client, resource_id, resource_type, start, end)
    return result.classification, result.evidence


def observe_activity_history(
    client: ArmClient,
    resource_id: str,
    resource_type: str,
    metrics: tuple[str, ...] | None,
    end: datetime,
    days: int = 90,
) -> ActivityObservation:
    rule = RULES.get(resource_type.casefold()) or ACTIVITY_RULES.get(resource_type.casefold())
    if not rule:
        return ActivityObservation()
    start = end - timedelta(days=days)
    try:
        if metrics is None:
            selected = resolve_metrics(metric_definitions(client, resource_id), rule)
            if not selected:
                return ActivityObservation(
                    "Unavailable",
                    f"{rule.activity_kind}: {rule.label}",
                    "Required activity metrics are not all exposed",
                    0,
                    days,
                )
            metrics = tuple(selected)
        if not metrics:
            return ActivityObservation()
        aggregations = dict(zip(metrics, rule.aggregations or ("Total",) * len(metrics), strict=True))
        totals: dict[str, MetricEvidence] = {}
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=30), end)
            chunk = metric_totals(
                client, resource_id, list(metrics), chunk_start, chunk_end, aggregations, rule.interval
            )
            for name, item in chunk.items():
                previous = totals.get(name)
                if previous is None:
                    totals[name] = item
                    continue
                last_nonzero_at = max(
                    (
                        timestamp for timestamp in (previous.last_nonzero_at, item.last_nonzero_at)
                        if timestamp is not None
                    ),
                    default=None,
                )
                totals[name] = MetricEvidence(
                    previous.total + item.total,
                    previous.covered_days + item.covered_days,
                    previous.expected_days + item.expected_days,
                    item.aggregation,
                    item.interval,
                    last_nonzero_at,
                )
            chunk_start = chunk_end
        return summarize_activity(rule, metrics, totals, days)
    except Exception as exc:
        LOG.warning("Historical activity query failed for %s: %s", resource_id, exc)
        return ActivityObservation(
            "Unavailable",
            f"{rule.activity_kind} via Azure Monitor metrics: {', '.join(metrics)}",
            f"90-day activity history could not be queried ({type(exc).__name__})",
            0,
            days,
        )


def idle_likelihood_from_activity(activity: ActivityObservation, end: datetime) -> tuple[str, str]:
    if (
        activity.last_observed_at.startswith("Not observed")
        and activity.expected_days > 0
        and activity.covered_days == activity.expected_days
    ):
        return "High", f"No nonzero usage observed with complete {activity.expected_days}-day coverage"
    try:
        last_observed_at = datetime.fromisoformat(activity.last_observed_at.replace("Z", "+00:00"))
    except ValueError:
        return "Not assessed", "No complete resource-specific activity evidence is available"
    age_days = max(0, (end.astimezone(timezone.utc).date() - last_observed_at.date()).days)
    if age_days <= 30:
        return "Low", f"Nonzero usage was observed {age_days} days ago"
    if activity.covered_days != activity.expected_days:
        return "Medium", f"Last observed usage was {age_days} days ago, but history coverage is incomplete"
    if age_days <= 60:
        return "Medium", f"Last observed nonzero usage was {age_days} days ago"
    return "High", f"Last observed nonzero usage was {age_days} days ago with complete history coverage"


def enrich_activity_history(
    client: ArmClient,
    targets: list[tuple[Finding, tuple[str, ...] | None]],
    end: datetime,
    days: int = 90,
) -> None:
    if not targets:
        return
    LOG.info("Enriching %d non-active resources with %d-day usage history", len(targets), days)
    for completed, (finding, metrics) in enumerate(targets, start=1):
        activity = observe_activity_history(
            client, finding.resource_id, finding.resource_type, metrics, end, days
        )
        finding.last_observed_activity_at = activity.last_observed_at
        finding.activity_signal = activity.signal
        finding.activity_observation = activity.evidence
        if finding.classification == "Unknown":
            finding.idle_likelihood, finding.likelihood_basis = idle_likelihood_from_activity(activity, end)
        if completed % 10 == 0 or completed == len(targets):
            LOG.info("Usage-history progress: %d/%d resources", completed, len(targets))


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
            f"<td>{html.escape(item.classification)}</td><td>{html.escape(item.idle_likelihood)}</td>"
            f"<td>{html.escape(item.currency)} {item.cost:,.2f}</td>"
            f'<td><a href="https://portal.azure.com/#resource{quote(item.resource_id, safe="/")}">{html.escape(item.resource_name)}</a></td>'
            f"<td>{html.escape(item.resource_type)}</td><td>{html.escape(item.resource_group)}</td>"
            f"<td>{html.escape(item.last_observed_activity_at)}</td>"
            f"<td>{html.escape(item.activity_signal)}</td>"
            f"<td>{html.escape(item.activity_observation)}</td>"
            f"<td>{html.escape(item.likelihood_basis)}</td>"
            f"<td>{html.escape(item.evidence)}</td></tr>"
            for item in sorted(items, key=lambda item: item.cost, reverse=True)
        )
        if not rows:
            rows = '<tr><td colspan="11">None.</td></tr>'
        sections.append(f"""<h3>{title}: {len(items)}</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<thead><tr><th>Status</th><th>Idle likelihood</th><th>30d cost</th><th>Resource</th><th>Type</th><th>Resource group</th><th>Last observed activity (UTC)</th><th>Activity signal</th><th>90d activity observation</th><th>Likelihood basis</th><th>30d usage evidence</th></tr></thead>
<tbody>{rows}</tbody></table>""")
    return f"""<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif">
<h2>RAI Dev subscription cost and idle-resource report</h2>
<p>Window: {start.date()} (inclusive) to {end.date()} (exclusive), UTC. Threshold: &gt; USD {threshold:,.2f}.</p>
<p><strong>Read-only advisory:</strong> Review dependencies, retained data and ownership before any action. Unknown means insufficient evidence, not idle or safe to delete. Active resources are omitted.</p>
<p>Idle likelihood summarizes available usage evidence; <strong>Not assessed</strong> means there is no usable resource-specific evidence. Last observed activity is the latest nonzero Azure Monitor metric bucket in the stated window. Event Hubs, ACR, Search and ADX use data-plane signals; VM and ACI use workload proxies. "Not observed" is bounded by the 90-day window and requires full metric coverage.</p>
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
    history_targets: list[tuple[Finding, tuple[str, ...] | None]] = []
    candidates = [row for row in rows if float(row.get("Cost", 0) or 0) > threshold and str(row.get("ResourceId", "") or "").startswith("/subscriptions/")]
    LOG.info("Cost query returned %d rows; classifying %d resources above threshold", len(rows), len(candidates))
    for row in candidates:
        cost = float(row.get("Cost", 0) or 0)
        resource_id = str(row.get("ResourceId", "") or "")
        resource_type = str(row.get("ResourceType", "") or "")
        assessment = assess(client, resource_id, resource_type, start, end)
        activity = assessment.activity or ActivityObservation()
        finding = Finding(
            resource_id=resource_id,
            resource_group=str(row.get("ResourceGroupName", "") or ""),
            resource_type=resource_type,
            resource_name=resource_name(resource_id),
            cost=cost,
            currency=str(row.get("Currency", "USD") or "USD"),
            classification=assessment.classification,
            evidence=assessment.evidence,
            idle_likelihood=assessment.idle_likelihood,
            likelihood_basis=assessment.likelihood_basis,
            last_observed_activity_at=activity.last_observed_at,
            activity_signal=activity.signal,
            activity_observation=activity.evidence,
        )
        findings.append(finding)
        activity_rule = ACTIVITY_RULES.get(resource_type.casefold())
        if assessment.classification != "Active" and (assessment.metrics or activity_rule):
            history_targets.append((finding, assessment.metrics or None))
    enrich_activity_history(client, history_targets, end)
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