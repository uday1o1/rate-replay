from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
)
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "https://app.ratereplay.test"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def test_app(tmp_path: Path) -> FastAPI:
    return create_app(
        AppSettings.for_test(
            object_store_root=tmp_path / "objects",
            repository_root=ROOT,
        )
    )


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as value:
        yield value


async def _register(client: httpx.AsyncClient, username: str) -> tuple[str, str]:
    response = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["user"]["user_id"]), cast(str, response.json()["csrf_token"])


def _seed_july_profile(test_app: FastAPI, owner_user_id: str) -> str:
    app_state = cast(Any, test_app.state)
    import_id = secrets.token_hex(16)
    profile_id = secrets.token_hex(16)
    start = datetime(2026, 7, 1, 7, tzinfo=UTC)
    end = datetime(2026, 8, 1, 7, tzinfo=UTC)
    now = datetime.now(UTC)
    start_ns = int(start.timestamp()) * 1_000_000_000
    end_ns = int(end.timestamp()) * 1_000_000_000
    with app_state.session_factory.begin() as database:
        database.add(
            ImportRecord(
                id=import_id,
                owner_user_id=owner_user_id,
                state="CONFIRMED",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                adapter="TEST_CANONICAL",
                raw_content_hash="c" * 64,
                created_at=now,
                published_at=now,
                confirmed_at=now,
                profile_version_id=profile_id,
            )
        )
        database.add(
            ProfileVersionRecord(
                id=profile_id,
                owner_user_id=owner_user_id,
                import_id=import_id,
                content_hash="d" * 64,
                canonical_content=b"test-only-canonical-profile",
                billing_period_start_utc_ns=start_ns,
                billing_period_end_utc_ns=end_ns,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=86_400,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
        for index in range(31):
            database.add(
                ImportReadingRecord(
                    id=secrets.token_hex(16),
                    import_id=import_id,
                    start_utc_ns=start_ns + index * 86_400 * 1_000_000_000,
                    duration_seconds=86_400,
                    energy_wh=10_000,
                    flow_direction="IMPORT",
                    source_unit="Wh",
                    source_multiplier=0,
                    source_reading_type="TEST_INTERVAL_ENERGY",
                    source_service_category="ELECTRICITY",
                    source_commodity="ELECTRICITY",
                    source_accumulation_behavior="DELTA_DATA",
                    source_data_qualifier="NORMAL",
                    source_time_attribute="NOT_APPLICABLE",
                    source_local_time_parameters_hash=None,
                    source_timezone_offset_seconds=None,
                    source_dst_offset_seconds=None,
                    quality_flags_json="[]",
                )
            )
    return profile_id


def _payload(profile_id: str, *, total_cents: int = 11_000) -> dict[str, object]:
    return {
        "request_schema_version": "replay-operation-v1",
        "profile_version_id": profile_id,
        "tariff_version_id": "pge-e1-2026-07",
        "account_facts": {
            "schema_version": "account-facts-v1",
            "service_window": {"start": "2026-07-01", "end": "2026-08-01"},
            "service_provider": "PG&E",
            "service_mode": "BUNDLED",
            "meter_count": 1,
            "primary_meter_only": True,
            "income_tier": "TIER_3",
            "care_enrolled": False,
            "fera_enrolled": False,
            "medical_baseline": False,
            "cca_service": False,
            "direct_access_service": False,
            "active_bill_protection": False,
            "solar_or_export": False,
            "baseline_territory": "T",
            "baseline_quantity_code": "BASIC",
            "qualifying_technologies": [],
            "user_attested_at": "2026-07-01T00:00:00Z",
        },
        "current_bill_total_cents": total_cents,
        "user_unsupported_lines": [
            {
                "line_item_key": "local_tax",
                "description": "User-entered local tax",
                "amount_cents": 200,
            }
        ],
    }


@pytest.mark.anyio
async def test_authenticated_tariff_provenance_and_replay_path(
    client: httpx.AsyncClient, test_app: FastAPI
) -> None:
    unauthenticated = await client.get("/v1/tariffs")
    assert unauthenticated.status_code == 401
    owner_id, csrf = await _register(client, "replay_owner")
    profile_id = _seed_july_profile(test_app, owner_id)

    listed = await client.get("/v1/tariffs")
    assert listed.status_code == 200
    listed_items = listed.json()["items"]
    assert [item["plan_code"] for item in listed_items] == [
        "E-1",
        "E-ELEC",
        "E-TOU-C",
        "E-TOU-D",
        "EV2-A",
    ]
    assert all(
        item["utility"] == "PG&E"
        and item["admission_status"] == "ADMITTED"
        and item["admitted_service_windows"] == [["2026-07-01", "2026-08-01"]]
        and item["target_account_predicate_id"].startswith("pge-")
        and item["calculation_time_mode"] == "HISTORICAL_REPLAY"
        and item["comparison_admitted"] is False
        and item["optimization_admitted"] is False
        for item in listed_items
    )
    detail = await client.get("/v1/tariffs/pge-e1-2026-07")
    assert detail.status_code == 200
    assert detail.json()["admission"]["compiler_content_sha256"] == (
        "ae003e7717fbb8fa964aac75ba21efa737f4db54bdba2abcb90b1a22d81a0016"
    )
    assert len(detail.json()["compilation"]["reports"]["source_coverage"]) == 2

    missing_csrf = await client.post(
        "/v1/replays",
        headers={"Origin": ORIGIN, "Idempotency-Key": "replay-request-one"},
        json=_payload(profile_id),
    )
    assert missing_csrf.status_code == 403
    created = await client.post(
        "/v1/replays",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "replay-request-one",
        },
        json=_payload(profile_id),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["repeated"] is False
    assert body["result"]["supported_calculated_cents"] == 9_819
    assert body["result"]["reconciliation"]["user_unsupported_cents"] == 200
    assert body["result"]["reconciliation"]["unexplained_residual_cents"] == 981
    assert len(body["result"]["line_items"]) == 4
    assert len(body["result"]["provenance_sources"]) == 2

    repeated = await client.post(
        "/v1/replays",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "replay-request-one",
        },
        json=_payload(profile_id),
    )
    assert repeated.status_code == 201
    assert repeated.json()["repeated"] is True
    assert repeated.json()["replay_id"] == body["replay_id"]
    fetched = await client.get(f"/v1/replays/{body['replay_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["result"] == body["result"]

    reused = await client.post(
        "/v1/replays",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "replay-request-one",
        },
        json=_payload(profile_id, total_cents=11_001),
    )
    assert reused.status_code == 409
    assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    app_state = cast(Any, test_app.state)
    with app_state.session_factory() as database:
        assert database.scalar(select(func.count()).select_from(ReplayResultRecord)) == 1
        assert database.scalar(select(func.count()).select_from(CalculationManifestRecord)) == 1
        job = database.get(JobRecord, body["job_id"])
        assert job is not None and job.kind == "REPLAY" and job.state == "SUCCEEDED"
        attempt = database.scalar(
            select(JobAttemptRecord).where(JobAttemptRecord.job_id == body["job_id"])
        )
        assert attempt is not None and attempt.state == "SUCCEEDED"


@pytest.mark.anyio
async def test_replay_resources_are_owner_scoped(
    client: httpx.AsyncClient, test_app: FastAPI
) -> None:
    first_owner, first_csrf = await _register(client, "first_replay_owner")
    profile_id = _seed_july_profile(test_app, first_owner)
    created = await client.post(
        "/v1/replays",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": first_csrf,
            "Idempotency-Key": "owner-replay-one",
        },
        json=_payload(profile_id),
    )
    assert created.status_code == 201

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        _, second_csrf = await _register(other, "second_replay_owner")
        denied_get = await other.get(f"/v1/replays/{created.json()['replay_id']}")
        assert denied_get.status_code == 404
        denied_post = await other.post(
            "/v1/replays",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": second_csrf,
                "Idempotency-Key": "owner-replay-two",
            },
            json=_payload(profile_id),
        )
        assert denied_post.status_code == 404
        assert denied_post.json()["code"] == "PROFILE_NOT_FOUND"


@pytest.mark.anyio
async def test_profile_window_must_match_admitted_account_facts(
    client: httpx.AsyncClient, test_app: FastAPI
) -> None:
    owner_id, csrf = await _register(client, "window_replay_owner")
    profile_id = _seed_july_profile(test_app, owner_id)
    payload = _payload(profile_id)
    cast(dict[str, Any], payload["account_facts"])["service_window"] = {
        "start": "2026-07-02",
        "end": "2026-08-01",
    }
    response = await client.post(
        "/v1/replays",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "window-replay-one",
        },
        json=payload,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PROFILE_ACCOUNT_WINDOW_MISMATCH"
