from datetime import datetime, timezone

from monitor import Finding, MetricRule, metric_totals, render_html, resolve_metrics


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


def test_html_only_lists_idle_candidates() -> None:
    findings = [
        Finding("/idle", "rg", "type", "idle-resource", 200.0, "USD", "Idle candidate", "usage=0"),
        Finding("/active", "rg", "type", "active-resource", 300.0, "USD", "Active", "usage=1"),
        Finding("/unknown", "rg", "type", "unknown-resource", 400.0, "USD", "Unknown", "unsupported"),
    ]
    value = render_html(findings, datetime.now(timezone.utc), datetime.now(timezone.utc), 100)
    assert "idle-resource" in value
    assert "active-resource" not in value
    assert "unknown-resource" not in value
    assert "Idle candidates: 1" in value


def test_metric_timespan_uses_z_not_numeric_offset() -> None:
    class FakeClient:
        def request(self, method, url, **kwargs):
            assert kwargs["params"]["timespan"].endswith("Z/2026-08-29T04:39:33Z")
            assert "+" not in kwargs["params"]["timespan"]

            class Response:
                @staticmethod
                def json():
                    return {"value": []}

            return Response()

    metric_totals(
        FakeClient(),
        "/subscriptions/s/resourceGroups/r/providers/Microsoft.EventHub/namespaces/e",
        ["IncomingMessages"],
        datetime(2026, 7, 30, 4, 39, 33, tzinfo=timezone.utc),
        datetime(2026, 8, 29, 4, 39, 33, tzinfo=timezone.utc),
    )