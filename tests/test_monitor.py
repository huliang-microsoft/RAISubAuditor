from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests
import monitor

from monitor import ArmClient, RULES, Finding, MetricRule, classify, cost_rows, metric_totals, render_html, resolve_metrics, retry_delay, send_report


def test_cost_rows_reads_all_pages_with_each_pages_columns() -> None:
    next_url = "https://management.azure.com/next?skiptoken=page2"
    client = Mock()
    client.request.side_effect = [
        Mock(json=lambda: {"properties": {
            "columns": [{"name": "Cost"}, {"name": "ResourceId"}],
            "rows": [[101, "/first"]], "nextLink": next_url,
        }}),
        Mock(json=lambda: {"properties": {
            "columns": [{"name": "ResourceId"}, {"name": "Cost"}],
            "rows": [["/second", 202]], "nextLink": None,
        }}),
    ]
    window_end = datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert cost_rows(client, "subscription", window_end - timedelta(days=30), window_end) == [
        {"Cost": 101, "ResourceId": "/first"}, {"Cost": 202, "ResourceId": "/second"},
    ]
    first_call, second_call = client.request.call_args_list
    assert second_call.args == ("POST", next_url)
    assert first_call.kwargs["json"] == second_call.kwargs["json"]
    assert second_call.kwargs["retry_budget"] == 1200
    assert second_call.kwargs["json"]["timePeriod"]["to"] == "2026-09-06T23:59:59+00:00"


@pytest.mark.parametrize("next_url", ["https://example.com/next", "//example.com/next"])
def test_cost_rows_rejects_untrusted_next_link(next_url) -> None:
    client = Mock()
    client.request.return_value.json.return_value = {
        "properties": {"columns": [], "rows": [], "nextLink": next_url},
    }
    now = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="non-ARM"):
        cost_rows(client, "subscription", now, now)
    assert client.request.call_count == 1


def test_cost_rows_rejects_pagination_loop() -> None:
    client = Mock()
    client.request.return_value.json.return_value = {"properties": {
        "columns": [], "rows": [], "nextLink": "https://management.azure.com/repeated",
    }}
    now = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="repeated"):
        cost_rows(client, "subscription", now, now)
    assert client.request.call_count == 2


def test_resolve_metrics_accepts_aliases() -> None:
    rule = MetricRule("test", (("IncomingMessages",), ("Network In Total", "NetworkInTotal")))
    definitions = {"incomingmessages": "IncomingMessages", "networkintotal": "NetworkInTotal"}
    assert resolve_metrics(definitions, rule) == ["IncomingMessages", "NetworkInTotal"]


def test_resolve_metrics_rejects_incomplete_evidence() -> None:
    rule = MetricRule("test", (("IncomingMessages",), ("OutgoingMessages",)))
    assert resolve_metrics({"incomingmessages": "IncomingMessages"}, rule) is None


def test_html_escapes_resource_values() -> None:
    finding = Finding("/x", "rg", "type", "<unsafe>", 123.0, "USD", "Idle candidate", "missing & unknown")
    value = render_html([finding], datetime.now(timezone.utc), datetime.now(timezone.utc), 100)
    assert "&lt;unsafe&gt;" in value
    assert "missing &amp; unknown" in value
    assert "<unsafe>" not in value


def test_html_lists_idle_and_unknown_above_threshold() -> None:
    findings = [
        Finding("/idle", "rg", "type", "idle-resource", 200.0, "USD", "Idle candidate", "usage=0"),
        Finding("/active", "rg", "type", "active-resource", 300.0, "USD", "Active", "usage=1"),
        Finding("/unknown", "rg", "type", "unknown-resource", 400.0, "USD", "Unknown", "unsupported"),
        Finding("/cheap", "rg", "type", "cheap-unknown", 99.0, "USD", "Unknown", "unsupported"),
        Finding("/boundary", "rg", "type", "boundary-unknown", 100.0, "USD", "Unknown", "unsupported"),
    ]
    value = render_html(findings, datetime.now(timezone.utc), datetime.now(timezone.utc), 100)
    assert "idle-resource" in value
    assert "active-resource" not in value
    assert "unknown-resource" in value
    assert "cheap-unknown" not in value
    assert "boundary-unknown" not in value
    assert "Idle candidates: 1" in value
    assert "Unknown: review required: 1" in value
    assert "not idle or safe to delete" in value


def test_metric_timespan_uses_z_not_numeric_offset() -> None:
    class FakeClient:
        def request(self, method, url, **kwargs):
            assert kwargs["params"]["timespan"].endswith("Z/2026-08-29T00:00:00Z")
            assert "+" not in kwargs["params"]["timespan"]

            class Response:
                @staticmethod
                def json():
                    return {"value": [], "interval": "P1D"}

            return Response()

    metric_totals(
        FakeClient(),
        "/subscriptions/s/resourceGroups/r/providers/Microsoft.EventHub/namespaces/e",
        ["IncomingMessages"],
        datetime(2026, 7, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 29, tzinfo=timezone.utc),
    )


START = datetime(2026, 8, 8, tzinfo=timezone.utc)
END = START + timedelta(days=30)
EVENTHUB = "microsoft.eventhub/namespaces"


def daily_metrics(resource_type=EVENTHUB, days=30, value=0):
    rule = RULES[resource_type]
    aggregations = rule.aggregations or ("Total",) * len(rule.aliases)
    return [{
        "name": {"value": aliases[0]},
        "timeseries": [{"data": [
            {"timeStamp": (START + timedelta(days=offset)).isoformat(), aggregation.lower(): value}
            for offset in range(days)
        ]}],
    } for aliases, aggregation in zip(rule.aliases, aggregations, strict=True)]


def classification_client(metrics, resource_type=EVENTHUB, interval="P1D"):
    definitions = [{"name": {"value": group[0]}} for group in RULES[resource_type].aliases]
    client = Mock()

    def request(method, url, **kwargs):
        if "metricDefinitions?" in url:
            return Mock(json=lambda: {"value": definitions})
        assert kwargs["params"]["autoAdjustTimegrain"] == "false"
        names = kwargs["params"]["metricnames"].split(",")
        return Mock(json=lambda: {"interval": interval, "value": [
            metric for metric in metrics if metric["name"]["value"] in names
        ]})

    client.request.side_effect = request
    return client


def test_full_zero_coverage_is_idle() -> None:
    result, evidence = classify(classification_client(daily_metrics()), "/resource", EVENTHUB, START, END)
    assert result == "Idle candidate"
    assert "coverage=30/30 days" in evidence


@pytest.mark.parametrize("days", [0, 1, 29])
def test_partial_zero_coverage_is_unknown(days) -> None:
    result, evidence = classify(classification_client(daily_metrics(days=days)), "/resource", EVENTHUB, START, END)
    assert result == "Unknown"
    assert f"coverage={days}/30 days" in evidence


def test_duplicate_days_do_not_fill_gaps() -> None:
    metrics = daily_metrics(days=1)
    for metric in metrics:
        metric["timeseries"][0]["data"] *= 30
    assert classify(classification_client(metrics), "/resource", EVENTHUB, START, END)[0] == "Unknown"


def test_missing_series_is_not_ignored() -> None:
    metrics = daily_metrics()
    metrics[0]["timeseries"].append({"data": []})
    assert classify(classification_client(metrics), "/resource", EVENTHUB, START, END)[0] == "Unknown"


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_invalid_values_never_prove_idle(value) -> None:
    metrics = daily_metrics()
    metrics[0]["timeseries"][0]["data"][5]["total"] = value
    assert classify(classification_client(metrics), "/resource", EVENTHUB, START, END)[0] == "Unknown"


def test_nonzero_partial_evidence_is_active() -> None:
    metrics = daily_metrics(days=1, value=2)
    assert classify(classification_client(metrics), "/resource", EVENTHUB, START, END)[0] == "Active"


def test_count_is_not_used_as_fallback_for_total() -> None:
    metrics = daily_metrics()
    for metric in metrics:
        for point in metric["timeseries"][0]["data"]:
            point["count"] = 0
            del point["total"]
    assert classify(classification_client(metrics), "/resource", EVENTHUB, START, END)[0] == "Unknown"


def test_wrong_interval_is_unknown() -> None:
    client = classification_client(daily_metrics(), interval="PT1H")
    assert classify(client, "/resource", EVENTHUB, START, END)[0] == "Unknown"


def test_adx_uses_query_result_count_and_ingestion_total() -> None:
    resource_type = "microsoft.kusto/clusters"
    client = classification_client(daily_metrics(resource_type), resource_type)
    assert classify(client, "/resource", resource_type, START, END)[0] == "Idle candidate"
    query_call, ingestion_call = client.request.call_args_list[1:]
    assert query_call.kwargs["params"]["metricnames"] == "QueryResult"
    assert query_call.kwargs["params"]["aggregation"] == "Count"
    assert ingestion_call.kwargs["params"]["aggregation"] == "Total"


@pytest.mark.parametrize("missing_hour", [False, True])
def test_aci_requires_all_hours_for_daily_coverage(missing_hour) -> None:
    resource_type = "microsoft.containerinstance/containergroups"
    metrics = [{
        "name": {"value": aliases[0]},
        "timeseries": [{"data": [
            {"timeStamp": (START + timedelta(hours=offset)).isoformat(), "average": 0}
            for offset in range(30 * 24) if not (missing_hour and offset == 5)
        ]}],
    } for aliases in RULES[resource_type].aliases]
    client = classification_client(metrics, resource_type, interval="PT1H")
    classification, evidence = classify(client, "/resource", resource_type, START, END)
    assert classification == ("Unknown" if missing_hour else "Idle candidate")
    assert f"coverage={29 if missing_hour else 30}/30 days" in evidence
    assert client.request.call_args.kwargs["params"]["interval"] == "PT1H"


def test_failed_metric_is_unknown() -> None:
    metrics = daily_metrics()
    metrics[0]["errorCode"] = "InvalidSamplingType"
    assert classify(classification_client(metrics), "/resource", EVENTHUB, START, END)[0] == "Unknown"


def test_retry_delay_honors_largest_cost_management_header(monkeypatch) -> None:
    monkeypatch.setattr("monitor.random.uniform", lambda lower, upper: 0)
    assert retry_delay({
        "Retry-After": "5",
        "x-ms-ratelimit-microsoft.consumption-retry-after": "120",
        "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "180",
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "60",
    }, 30) == 180


@pytest.mark.parametrize("value", ["invalid", "-5", "nan", "inf"])
def test_invalid_retry_header_uses_backoff(monkeypatch, value) -> None:
    monkeypatch.setattr("monitor.random.uniform", lambda lower, upper: 0)
    assert retry_delay({"Retry-After": value}, 60) == 60


def test_retry_after_http_date(monkeypatch) -> None:
    from email.utils import format_datetime

    monkeypatch.setattr("monitor.random.uniform", lambda lower, upper: 0)
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    assert 118 <= retry_delay({"Retry-After": future}, 5) <= 120


def arm_response(status, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = b"{}"
    response._content_consumed = True
    return response


def test_arm_retries_cost_throttling_with_server_delay(monkeypatch) -> None:
    sleep = Mock()
    monkeypatch.setattr("monitor.time.sleep", sleep)
    monkeypatch.setattr("monitor.random.uniform", lambda lower, upper: 0)
    client = ArmClient(Mock())
    client.session = Mock()
    client.session.request.side_effect = [
        arm_response(429, {"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "180"}),
        arm_response(200),
    ]
    assert client.request("POST", "/providers/Microsoft.CostManagement/query", retry_budget=1200,
                          headers={"Custom": "preserved"}).status_code == 200
    sleep.assert_called_once_with(180)
    assert client.session.request.call_args.kwargs["headers"]["Custom"] == "preserved"


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError])
def test_arm_retries_transport_failures(monkeypatch, failure) -> None:
    monkeypatch.setattr("monitor.time.sleep", Mock())
    client = ArmClient(Mock())
    client.session = Mock()
    client.session.request.side_effect = [failure("transient"), arm_response(200)]
    assert client.request("GET", "/resource").status_code == 200
    assert client.session.request.call_count == 2


def test_arm_does_not_retry_auth_failure(monkeypatch) -> None:
    sleep = Mock()
    monkeypatch.setattr("monitor.time.sleep", sleep)
    client = ArmClient(Mock())
    client.session = Mock()
    client.session.request.return_value = arm_response(403)
    with pytest.raises(requests.HTTPError):
        client.request("GET", "/resource")
    sleep.assert_not_called()


def test_arm_does_not_sleep_past_budget(monkeypatch) -> None:
    sleep = Mock()
    monkeypatch.setattr("monitor.time.sleep", sleep)
    client = ArmClient(Mock())
    client.session = Mock()
    client.session.request.return_value = arm_response(429, {"Retry-After": "3600"})
    with pytest.raises(RuntimeError, match="budget exhausted"):
        client.request("POST", "/resource", retry_budget=1200)
    sleep.assert_not_called()


def test_arm_attempts_are_bounded(monkeypatch) -> None:
    sleep = Mock()
    monkeypatch.setattr("monitor.time.sleep", sleep)
    client = ArmClient(Mock())
    client.session = Mock()
    client.session.request.return_value = arm_response(503)
    with pytest.raises(RuntimeError, match="12 attempts"):
        client.request("GET", "/resource")
    assert client.session.request.call_count == 12
    assert sleep.call_count == 11


def test_send_report_requires_matching_confirmation(monkeypatch) -> None:
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"status": "Sent", "runId": "run1"}))
    monkeypatch.setattr("monitor.requests.post", post)
    send_report("https://example.com/trigger", {"runId": "run1"})
    assert post.call_count == 1


@pytest.mark.parametrize("status,body", [
    (202, {}), (502, {"status": "Failed"}),
    (200, {"status": "Sent", "runId": "different"}),
    (200, {"status": "Failed", "runId": "run1"}), (200, []),
])
def test_send_report_rejects_unconfirmed_delivery(monkeypatch, status, body) -> None:
    post = Mock(return_value=Mock(status_code=status, json=lambda: body))
    monkeypatch.setattr("monitor.requests.post", post)
    with pytest.raises(RuntimeError, match="Email|acknowledge"):
        send_report("https://example.com/trigger?sig=secret", {"runId": "run1"})
    assert post.call_count == 1


def test_send_report_does_not_log_secret_or_retry_ambiguous_timeout(monkeypatch) -> None:
    post = Mock(side_effect=requests.Timeout("https://example.com/trigger?sig=secret"))
    monkeypatch.setattr("monitor.requests.post", post)
    with pytest.raises(RuntimeError) as error:
        send_report("https://example.com/trigger?sig=secret", {"runId": "run1"})
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
    assert post.call_count == 1


def configure_main(monkeypatch):
    for name, value in {
        "SUBSCRIPTION_ID": "test-subscription", "EMAIL_TO": "owner@example.com",
        "STORAGE_ACCOUNT": "reportsaccount", "REPORT_CONTAINER": "reports",
        "LOGIC_APP_TRIGGER_URL": "https://example.com/trigger", "COST_THRESHOLD_USD": "100",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("monitor.DefaultAzureCredential", Mock())
    monkeypatch.setattr("monitor.ArmClient", Mock())
    upload = Mock(return_value={"json": "https://example.com/report.json"})
    send = Mock()
    monkeypatch.setattr("monitor.upload_reports", upload)
    monkeypatch.setattr("monitor.send_report", send)
    return upload, send


def test_main_reports_high_cost_unknown_and_keeps_complete_day_window(monkeypatch) -> None:
    import json

    upload, send = configure_main(monkeypatch)
    costs = Mock(return_value=[{
        "ResourceId": "/subscriptions/test-subscription/resourceGroups/rg/providers/unsupported/type/resource",
        "ResourceType": "unsupported/type", "ResourceGroupName": "rg", "Cost": 101, "Currency": "USD",
    }])
    monkeypatch.setattr("monitor.cost_rows", costs)
    monitor.main()
    window_start, window_end = costs.call_args.args[-2:]
    assert window_end - window_start == timedelta(days=30)
    assert window_start.hour == window_end.hour == window_start.minute == window_end.minute == 0
    artifacts = upload.call_args.args[-1]
    report = json.loads(artifacts["json"][0])
    assert report["findings"][0]["classification"] == "Unknown"
    payload = send.call_args.args[1]
    assert "1 unknown" in payload["subject"]
    assert "Unknown: review required: 1" in payload["html"]
    assert payload["runId"] == report["runId"]
    assert set(artifacts) == {"json", "csv", "html"}


def test_cost_failure_prevents_report_and_success_email(monkeypatch) -> None:
    upload, send = configure_main(monkeypatch)
    monkeypatch.setattr("monitor.cost_rows", Mock(side_effect=RuntimeError("throttled")))
    with pytest.raises(RuntimeError, match="throttled"):
        monitor.main()
    upload.assert_not_called()
    send.assert_not_called()


def test_blob_failure_prevents_success_email(monkeypatch) -> None:
    upload, send = configure_main(monkeypatch)
    monkeypatch.setattr("monitor.cost_rows", Mock(return_value=[]))
    upload.side_effect = RuntimeError("storage failed")
    with pytest.raises(RuntimeError, match="storage failed"):
        monitor.main()
    send.assert_not_called()


def test_notification_failure_fails_main(monkeypatch) -> None:
    upload, send = configure_main(monkeypatch)
    monkeypatch.setattr("monitor.cost_rows", Mock(return_value=[]))
    send.side_effect = RuntimeError("Email not confirmed")
    with pytest.raises(RuntimeError, match="not confirmed"):
        monitor.main()
    upload.assert_called_once()


def test_send_report_rejects_invalid_json(monkeypatch) -> None:
    post = Mock(return_value=Mock(status_code=200))
    post.return_value.json.side_effect = ValueError("invalid JSON")
    monkeypatch.setattr("monitor.requests.post", post)
    with pytest.raises(RuntimeError, match="invalid email acknowledgement"):
        send_report("https://example.com/trigger", {"runId": "run1"})