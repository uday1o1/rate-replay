from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.models import (
    ComparisonResultRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    ProfileVersionRecord,
    UserRecord,
)
from ratereplay_tariffs.comparison import compare_admitted_tariffs, load_required_component_keys
from ratereplay_worker.comparison_worker import ComparisonWorker
from ratereplay_worker.replay_worker import ReplayWorker
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "https://app.ratereplay.test"
PASSWORD = "correct horse battery staple"
PROFILE_HASH = "47b449f47039960cde24666a5ed2723781b7773d624dbdd2b74de78e02da19ce"
CANDIDATE_IDS = (
    "pge-e1-2026-07",
    "pge-eelec-2026-07",
    "pge-etouc-2026-07",
    "pge-etoud-2026-07",
    "pge-ev2a-2026-07",
)


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


def _comparison_facts() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
        ),
    )


def _seed_demo_profile(test_app: FastAPI, owner_user_id: str) -> str:
    profile = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "data/demo/july-2026-simulated-profile.json").read_text(encoding="utf-8")
        ),
    )
    readings = cast(list[dict[str, Any]], profile["readings"])
    import_id = secrets.token_hex(16)
    profile_id = secrets.token_hex(16)
    start = datetime.fromisoformat(cast(str, readings[0]["start_utc"]).replace("Z", "+00:00"))
    final_start = datetime.fromisoformat(
        cast(str, readings[-1]["start_utc"]).replace("Z", "+00:00")
    )
    end = final_start.timestamp() + cast(int, readings[-1]["duration_seconds"])
    now = datetime.now(UTC)
    app_state = cast(Any, test_app.state)
    with app_state.session_factory.begin() as database:
        database.add(
            ImportRecord(
                id=import_id,
                owner_user_id=owner_user_id,
                state="CONFIRMED",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                adapter="SIMULATED_PROFILE_V1",
                raw_content_hash=PROFILE_HASH,
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
                content_hash=PROFILE_HASH,
                canonical_content=b"frozen-july-2026-simulated-profile",
                billing_period_start_utc_ns=int(start.timestamp()) * 1_000_000_000,
                billing_period_end_utc_ns=int(end) * 1_000_000_000,
                tariff_timezone=cast(str, profile["tariff_timezone"]),
                interval_resolution_seconds=cast(int, profile["interval_resolution_seconds"]),
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
        database.add_all(
            [
                ImportReadingRecord(
                    id=secrets.token_hex(16),
                    import_id=import_id,
                    start_utc_ns=int(
                        datetime.fromisoformat(
                            cast(str, reading["start_utc"]).replace("Z", "+00:00")
                        ).timestamp()
                    )
                    * 1_000_000_000,
                    duration_seconds=cast(int, reading["duration_seconds"]),
                    energy_wh=cast(int, reading["energy_wh"]),
                    flow_direction="IMPORT",
                    source_unit="Wh",
                    source_multiplier=0,
                    source_reading_type="SIMULATED_INTERVAL_ENERGY",
                    source_service_category="ELECTRICITY",
                    source_commodity="ELECTRICITY",
                    source_accumulation_behavior="DELTA_DATA",
                    source_data_qualifier="SIMULATED",
                    source_time_attribute="NOT_APPLICABLE",
                    source_local_time_parameters_hash=None,
                    source_timezone_offset_seconds=None,
                    source_dst_offset_seconds=None,
                    quality_flags_json="[]",
                )
                for reading in readings
            ]
        )
    return profile_id


def _replay_payload(profile_id: str) -> dict[str, object]:
    account_facts = _comparison_facts()["account_facts"]
    return {
        "request_schema_version": "replay-operation-v1",
        "profile_version_id": profile_id,
        "tariff_version_id": "pge-e1-2026-07",
        "account_facts": account_facts,
        "current_bill_total_cents": 30_000,
        "user_unsupported_lines": [
            {
                "line_item_key": "local_tax",
                "description": "Current bill only",
                "amount_cents": 300,
            }
        ],
    }


def _comparison_payload(replay_id: str) -> dict[str, object]:
    facts = _comparison_facts()
    return {
        "request_schema_version": "comparison-operation-v1",
        "replay_id": replay_id,
        "candidate_tariff_version_ids": list(CANDIDATE_IDS),
        "account_facts": facts["account_facts"],
        "dated_eligibility_facts": facts["dated_eligibility_facts"],
    }


async def _create_replay(
    client: httpx.AsyncClient, test_app: FastAPI, username: str
) -> tuple[str, str, str]:
    owner_id, csrf = await _register(client, username)
    profile_id = _seed_demo_profile(test_app, owner_id)
    response = await client.post(
        "/v1/replays",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": f"{username}-replay",
        },
        json=_replay_payload(profile_id),
    )
    assert response.status_code == 202, response.text
    state = cast(Any, test_app.state)
    worker = ReplayWorker(
        worker_id="comparison-replay-worker",
        session_factory=state.session_factory,
        jobs=state.job_service,
        artifacts=ArtifactService(state.session_factory, state.object_store),
        admitted_tariffs=state.admitted_tariffs,
        environment_lock_hash=state.environment_lock_hash,
    )
    assert worker.run_once(now=datetime.now(UTC))
    job = await client.get(f"/v1/jobs/{response.json()['job_id']}")
    assert job.status_code == 200 and job.json()["state"] == "SUCCEEDED"
    return cast(str, job.json()["terminal_result_id"]), owner_id, csrf


def _run_comparison_worker(test_app: FastAPI) -> None:
    state = cast(Any, test_app.state)
    worker = ComparisonWorker(
        worker_id="api-comparison-test-worker",
        session_factory=state.session_factory,
        jobs=state.job_service,
        artifacts=ArtifactService(state.session_factory, state.object_store),
        admitted_tariffs=state.admitted_tariffs,
        required_component_keys=load_required_component_keys(ROOT),
        environment_lock_hash=state.environment_lock_hash,
    )
    assert worker.run_once(now=datetime.now(UTC))


@pytest.mark.anyio
async def test_rankable_comparison_is_immutable_idempotent_and_owner_scoped(
    client: httpx.AsyncClient, test_app: FastAPI
) -> None:
    replay_id, owner_id, csrf = await _create_replay(client, test_app, "comparison_owner")
    missing_csrf = await client.post(
        "/v1/comparisons",
        headers={"Origin": ORIGIN, "Idempotency-Key": "comparison-request-one"},
        json=_comparison_payload(replay_id),
    )
    assert missing_csrf.status_code == 403

    created = await client.post(
        "/v1/comparisons",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "comparison-request-one",
        },
        json=_comparison_payload(replay_id),
    )
    assert created.status_code == 202, created.text
    submitted = created.json()
    assert submitted["state"] == "QUEUED"
    assert submitted["repeated"] is False
    assert len(submitted["operation_request_hash"]) == 64
    assert len(submitted["semantic_hash"]) == 64
    _run_comparison_worker(test_app)
    completed = await client.get(f"/v1/jobs/{submitted['job_id']}")
    assert completed.status_code == 200
    assert completed.json()["state"] == "SUCCEEDED"
    assert completed.json()["terminal_result_type"] == "COMPARISON"
    comparison_id = cast(str, completed.json()["terminal_result_id"])
    fetched = await client.get(f"/v1/comparisons/{comparison_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    result = body["result"]
    assert body["owner_user_id"] == owner_id
    assert body["current_replay_id"] == replay_id
    assert body["repeated"] is False
    assert result["rankable"] is True
    assert result["ranked_tariff_version_ids"] == [
        "pge-etoud-2026-07",
        "pge-ev2a-2026-07",
        "pge-e1-2026-07",
        "pge-etouc-2026-07",
        "pge-eelec-2026-07",
    ]
    assert result["winner_tariff_version_ids"] == ["pge-etoud-2026-07"]
    assert result["savings_against_current_supported_cents"] == 1_707
    assert all(
        candidate["eligibility"]["status"] == "ELIGIBLE" for candidate in result["candidates"]
    )
    assert all(
        "reconciliation" not in candidate["alternative_plan"]
        and "user_unsupported_lines" not in candidate["alternative_plan"]
        for candidate in result["candidates"]
    )
    assert all(
        candidate["alternative_plan"]["provenance_sources"] and candidate["component_coverage"]
        for candidate in result["candidates"]
    )

    repeated_payload = _comparison_payload(replay_id)
    cast(list[str], repeated_payload["candidate_tariff_version_ids"]).reverse()
    repeated = await client.post(
        "/v1/comparisons",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "comparison-request-one",
        },
        json=repeated_payload,
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["repeated"] is True
    assert repeated.json()["job_id"] == submitted["job_id"]
    assert repeated.json()["terminal_result_id"] == comparison_id

    changed = _comparison_payload(replay_id)
    changed["candidate_tariff_version_ids"] = ["pge-e1-2026-07", "pge-etouc-2026-07"]
    conflict = await client.post(
        "/v1/comparisons",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "comparison-request-one",
        },
        json=changed,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    app_state = cast(Any, test_app.state)
    with app_state.session_factory() as database:
        assert database.scalar(select(func.count()).select_from(ComparisonResultRecord)) == 1
        record = database.get(ComparisonResultRecord, body["comparison_id"])
        assert record is not None and record.result_hash == result["comparison_sha256"]
        job = database.get(JobRecord, submitted["job_id"])
        assert job is not None and job.kind == "COMPARISON" and job.state == "SUCCEEDED"
        attempt = database.scalar(
            select(JobAttemptRecord).where(JobAttemptRecord.job_id == submitted["job_id"])
        )
        assert attempt is not None and attempt.state == "SUCCEEDED"

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        _, other_csrf = await _register(other, "comparison_intruder")
        denied_get = await other.get(f"/v1/comparisons/{body['comparison_id']}")
        assert denied_get.status_code == 404
        denied_post = await other.post(
            "/v1/comparisons",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": other_csrf,
                "Idempotency-Key": "cross-owner-comparison",
            },
            json=_comparison_payload(replay_id),
        )
        assert denied_post.status_code == 404
        assert denied_post.json()["code"] == "REPLAY_NOT_FOUND"


@pytest.mark.anyio
async def test_unknown_eligibility_blocks_ranking_and_account_mutation_is_rejected(
    client: httpx.AsyncClient, test_app: FastAPI
) -> None:
    replay_id, _, csrf = await _create_replay(client, test_app, "blocked_comparison_owner")
    blocked_payload = _comparison_payload(replay_id)
    dated = cast(dict[str, object], blocked_payload["dated_eligibility_facts"])
    dated["annual_usage_wh"] = None
    blocked = await client.post(
        "/v1/comparisons",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "blocked-comparison-one",
        },
        json=blocked_payload,
    )
    assert blocked.status_code == 202, blocked.text
    _run_comparison_worker(test_app)
    completed = await client.get(f"/v1/jobs/{blocked.json()['job_id']}")
    assert completed.json()["state"] == "SUCCEEDED"
    fetched = await client.get(f"/v1/comparisons/{completed.json()['terminal_result_id']}")
    assert fetched.status_code == 200
    result = fetched.json()["result"]
    assert result["rankable"] is False
    assert result["ranked_tariff_version_ids"] == []
    assert result["winner_tariff_version_ids"] == []
    assert result["savings_against_current_supported_cents"] is None
    assert any(item["code"] == "CANDIDATE_ELIGIBILITY_UNKNOWN" for item in result["exclusions"])

    changed_account = _comparison_payload(replay_id)
    account = cast(dict[str, object], changed_account["account_facts"])
    account["baseline_territory"] = "P"
    rejected = await client.post(
        "/v1/comparisons",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "changed-account-comparison",
        },
        json=changed_account,
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "CURRENT_REPLAY_ACCOUNT_MISMATCH"


@pytest.mark.anyio
async def test_comparison_worker_rejects_tampered_durable_request(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    replay_id, _, csrf = await _create_replay(client, test_app, "tampered_comparison_owner")
    submitted = await client.post(
        "/v1/comparisons",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "tampered-comparison-one",
        },
        json=_comparison_payload(replay_id),
    )
    assert submitted.status_code == 202
    state = cast(Any, test_app.state)
    with state.session_factory.begin() as database:
        job = database.get(JobRecord, submitted.json()["job_id"])
        assert job is not None
        job.request_json = "{}"

    _run_comparison_worker(test_app)

    completed = await client.get(f"/v1/jobs/{submitted.json()['job_id']}")
    assert completed.json()["state"] == "FAILED"
    assert completed.json()["failure_code"] == "COMPARISON_REQUEST_INVALID"
    with state.session_factory() as database:
        assert database.scalar(select(func.count()).select_from(ComparisonResultRecord)) == 0


@pytest.mark.anyio
async def test_comparison_finalizer_loses_fence_after_account_generation_change(
    client: httpx.AsyncClient,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_id, owner_id, csrf = await _create_replay(client, test_app, "fenced_comparison_owner")
    submitted = await client.post(
        "/v1/comparisons",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "fenced-comparison-one",
        },
        json=_comparison_payload(replay_id),
    )
    assert submitted.status_code == 202
    state = cast(Any, test_app.state)
    original = cast(Callable[..., Any], compare_admitted_tariffs)

    def fence_during_calculation(*args: Any, **kwargs: Any) -> Any:
        with state.session_factory.begin() as database:
            owner = database.get(UserRecord, owner_id)
            assert owner is not None
            owner.lifecycle_generation += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        "ratereplay_worker.comparison_worker.compare_admitted_tariffs",
        fence_during_calculation,
    )

    _run_comparison_worker(test_app)

    completed = await client.get(f"/v1/jobs/{submitted.json()['job_id']}")
    assert completed.json()["state"] == "CANCELLED"
    assert completed.json()["failure_code"] == "SCOPE_FENCED"
    with state.session_factory() as database:
        assert database.scalar(select(func.count()).select_from(ComparisonResultRecord)) == 0
