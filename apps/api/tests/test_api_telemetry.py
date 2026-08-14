from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_domain.telemetry import Telemetry, TelemetryConfiguration

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "https://app.ratereplay.test"


class CapturingExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def captured_app(tmp_path: Path) -> tuple[FastAPI, CapturingExporter]:
    exporter = CapturingExporter()
    telemetry = Telemetry(
        TelemetryConfiguration(service_name="ratereplay-api", environment="test"),
        span_exporter=exporter,
    )
    app = create_app(
        AppSettings.for_test(
            object_store_root=tmp_path / "objects",
            deletion_ledger_root=tmp_path / "ledger",
            repository_root=ROOT,
        ),
        telemetry=telemetry,
    )
    return app, exporter


@pytest.fixture
async def client(
    captured_app: tuple[FastAPI, CapturingExporter],
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=captured_app[0])
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as value:
        yield value


@pytest.mark.anyio
async def test_http_metrics_and_traces_exclude_payloads_and_object_ids(
    client: httpx.AsyncClient,
    captured_app: tuple[FastAPI, CapturingExporter],
) -> None:
    sensitive_interval = "interval-energy-wh-8675309"
    sensitive_bill = "bill-total-31415926"
    sensitive_object_id = "deletion-object-27182818"
    response = await client.post(
        "/v1/replays",
        json={
            "request_schema_version": "replay-operation-v1",
            "profile_version_id": sensitive_interval,
            "tariff_version_id": sensitive_bill,
            "account_facts": {},
        },
    )
    assert response.status_code == 401
    deletion = await client.get(f"/v1/deletions/{sensitive_object_id}")
    assert deletion.status_code == 404
    metrics_response = await client.get("/metrics")
    assert metrics_response.status_code == 200
    metrics = metrics_response.text
    assert 'route="/v1/replays"' in metrics
    assert 'route="/v1/deletions/{deletion_id}"' in metrics
    assert metrics_response.headers["cache-control"] == "no-store"

    exporter = captured_app[1]
    assert len(exporter.spans) == 3
    rendered = "\n".join(f"{span.name}\n{span.attributes!r}" for span in exporter.spans)
    for sensitive in (sensitive_interval, sensitive_bill, sensitive_object_id):
        assert sensitive not in metrics
        assert sensitive not in rendered
    replay_attributes = exporter.spans[0].attributes
    deletion_attributes = exporter.spans[1].attributes
    assert replay_attributes is not None
    assert deletion_attributes is not None
    assert replay_attributes["http.route"] == "/v1/replays"
    assert deletion_attributes["http.route"] == "/v1/deletions/{deletion_id}"
    assert set(replay_attributes) == {
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "ratereplay.request.id",
    }
    assert isinstance(replay_attributes["ratereplay.request.id"], str)
    assert len(replay_attributes["ratereplay.request.id"]) == 24
