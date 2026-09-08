import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from azure.identity import AzureCliCredential
from azure.storage.blob import BlobClient

from monitor import ArmClient, RULES, metric_definitions, metric_totals, resolve_metrics


pytestmark = pytest.mark.skipif(
    not os.getenv("LIVE_REPORT_URL"), reason="Set LIVE_REPORT_URL to an existing private JSON report for read-only checks",
)


@pytest.fixture(scope="module")
def live_context():
    credential = AzureCliCredential()
    blob = BlobClient.from_blob_url(os.environ["LIVE_REPORT_URL"], credential=credential)
    report = json.loads(blob.download_blob().readall())
    return ArmClient(credential), report["findings"]


@pytest.mark.parametrize("resource_type", [
    "microsoft.eventhub/namespaces", "microsoft.kusto/clusters", "microsoft.search/searchservices",
])
def test_live_metric_names_aggregations_and_daily_interval(live_context, resource_type):
    client, findings = live_context
    resource = next(item for item in findings if item["resource_type"] == resource_type)
    rule = RULES[resource_type]
    selected = resolve_metrics(metric_definitions(client, resource["resource_id"]), rule)
    assert selected, f"Required metrics are not exposed for {resource_type}"
    aggregations = dict(zip(selected, rule.aggregations or ("Total",) * len(selected), strict=True))
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    totals = metric_totals(client, resource["resource_id"], selected, end - timedelta(days=30), end, aggregations)
    assert set(totals) == set(selected)
    for item in totals.values():
        assert item.expected_days == 30
        assert 0 <= item.covered_days <= 30