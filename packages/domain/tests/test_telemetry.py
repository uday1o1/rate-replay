from __future__ import annotations

from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode
from ratereplay_domain import telemetry as telemetry_module
from ratereplay_domain.telemetry import Telemetry, TelemetryConfiguration


class CapturingExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


def _telemetry(exporter: SpanExporter | None = None) -> Telemetry:
    return Telemetry(
        TelemetryConfiguration(
            service_name="ratereplay-worker",
            environment="test",
        ),
        span_exporter=exporter,
    )


def test_worker_telemetry_has_fixed_labels_and_no_error_message() -> None:
    exporter = CapturingExporter()
    telemetry = _telemetry(exporter)
    sensitive = "private-interval-8675309"

    with pytest.raises(RuntimeError, match=sensitive):
        telemetry.run_worker("REPLAY", lambda: _raise(sensitive))

    metrics = telemetry.prometheus_bytes().decode("utf-8")
    assert 'kind="REPLAY",outcome="error"' in metrics
    assert sensitive not in metrics
    assert len(exporter.spans) == 1
    rendered_span = repr(exporter.spans[0].attributes)
    assert exporter.spans[0].name == "worker.poll"
    assert exporter.spans[0].attributes == {
        "job.kind": "REPLAY",
        "worker.outcome": "error",
    }
    assert sensitive not in rendered_span


def test_unknown_labels_are_collapsed_instead_of_exported() -> None:
    telemetry = _telemetry()
    assert not telemetry.run_worker("private-owner-id", lambda: False)
    metrics = telemetry.prometheus_bytes().decode("utf-8")
    assert 'kind="UNKNOWN",outcome="idle"' in metrics
    assert "private-owner-id" not in metrics


def test_http_observation_collapses_unsafe_values_and_records_failures() -> None:
    exporter = CapturingExporter()
    telemetry = _telemetry(exporter)
    sensitive = "private-exception-8675309"

    with telemetry.http_request("CONNECT") as observation:
        observation.finish(route="/unsafe?owner=private", status_code=999)
        observation.finish(route="/ignored", status_code=204)
    with pytest.raises(RuntimeError, match=sensitive), telemetry.http_request("POST"):
        raise RuntimeError(sensitive)
    with telemetry.http_request("GET") as observation:
        observation.finish(route="/" + "a" * 129, status_code=200)

    metrics = telemetry.prometheus_bytes().decode("utf-8")
    assert 'method="OTHER",route="unmatched",status="500"' in metrics
    assert 'method="POST",route="unmatched",status="500"' in metrics
    assert sensitive not in metrics
    assert len(exporter.spans) == 3
    assert exporter.spans[1].status.status_code is StatusCode.ERROR
    assert sensitive not in repr(exporter.spans[1])


def test_telemetry_configuration_rejects_arbitrary_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATEREPLAY_TRACE_EXPORTER", "network-url-from-user")
    with pytest.raises(RuntimeError, match="must be 'none' or 'console'"):
        TelemetryConfiguration.from_environment(
            service_name="ratereplay-api",
            environment="test",
        )
    monkeypatch.setenv("RATEREPLAY_TRACE_EXPORTER", "none")
    with pytest.raises(ValueError, match="service name"):
        TelemetryConfiguration.from_environment(
            service_name="owner-name",
            environment="test",
        )
    with pytest.raises(ValueError, match="environment"):
        TelemetryConfiguration.from_environment(
            service_name="ratereplay-api",
            environment="owner-environment",
        )


def test_console_exporter_configuration_is_explicit_and_shutdown_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = CapturingExporter()
    monkeypatch.setenv("RATEREPLAY_TRACE_EXPORTER", "console")
    monkeypatch.setattr(telemetry_module, "ConsoleSpanExporter", lambda: exporter)
    configuration = TelemetryConfiguration.from_environment(
        service_name="ratereplay-worker",
        environment="staging",
    )
    telemetry = Telemetry(configuration)
    assert telemetry.run_worker("IMPORT", lambda: True)
    telemetry.shutdown()
    assert len(exporter.spans) == 1


def _raise(message: str) -> bool:
    raise RuntimeError(message)
