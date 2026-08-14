from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_optimizer.solver import (
    ExactOptimizationResult,
    ExactSearchStatus,
    optimize_exact,
)
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportReadingRecord,
    ImportRecord,
    JobRecord,
    ProfileVersionRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
    ScenarioResultRecord,
    UserRecord,
)
from ratereplay_worker.scenario_worker import ScenarioWorker
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


def _seed_hourly_july_profile(test_app: FastAPI, owner_user_id: str) -> str:
    app_state = cast(Any, test_app.state)
    import_id = secrets.token_hex(16)
    profile_id = secrets.token_hex(16)
    start = datetime(2026, 7, 1, 7, tzinfo=UTC)
    end = datetime(2026, 8, 1, 7, tzinfo=UTC)
    start_ns = int(start.timestamp()) * 1_000_000_000
    now = datetime.now(UTC)
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
                canonical_content=b"test-only-hourly-july-profile",
                billing_period_start_utc_ns=start_ns,
                billing_period_end_utc_ns=int(end.timestamp()) * 1_000_000_000,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=3_600,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
        for index in range(31 * 24):
            database.add(
                ImportReadingRecord(
                    id=secrets.token_hex(16),
                    import_id=import_id,
                    start_utc_ns=start_ns + index * 3_600 * 1_000_000_000,
                    duration_seconds=3_600,
                    energy_wh=100,
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


def _account_payload() -> tuple[dict[str, object], dict[str, object]]:
    payload = cast(
        dict[str, dict[str, object]],
        json.loads(
            (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
        ),
    )
    return payload["account_facts"], payload["dated_eligibility_facts"]


async def _scenario_payload(
    client: httpx.AsyncClient,
    profile_id: str,
) -> dict[str, object]:
    response = await client.get(f"/v1/profiles/{profile_id}/scenario-slots")
    assert response.status_code == 200, response.text
    slots = cast(list[dict[str, object]], response.json()["slots"])
    reference_start = 137
    reference_energy = {reference_start: 1_000, reference_start + 1: 1_000}
    account, dated = _account_payload()
    return {
        "request_schema_version": "scenario-operation-v1",
        "profile_version_id": profile_id,
        "tariff_version_id": "pge-etoud-2026-07",
        "account_facts": account,
        "dated_eligibility_facts": dated,
        "electrical_constraints": {
            "site_import_cap_w": 5_000,
            "flexible_load_aggregate_cap_w": 2_000,
            "energy_basis": "METER_SIDE",
        },
        "loads": [
            {
                "load_id": "00000000-0000-0000-0000-000000000001",
                "physical_asset_key": "browser-ev-1",
                "kind": "EV",
                "mode": "HISTORICAL_ADDITION",
                "execution_spec": {
                    "execution_type": "CONTIGUOUS_FIXED_SHAPE",
                    "fixed_slot_shape_wh": [1_000, 1_000],
                },
                "occurrences": [
                    {
                        "occurrence_id": "10000000-0000-0000-0000-000000000001",
                        "required_energy_wh": 2_000,
                        "earliest_start_utc": slots[135]["slot_start_utc"],
                        "deadline_utc": slots[139]["slot_start_utc"],
                        "reference_schedule": [
                            {
                                "slot_start_utc": slot["slot_start_utc"],
                                "duration_seconds": slot["duration_seconds"],
                                "energy_wh": reference_energy.get(index, 0),
                            }
                            for index, slot in enumerate(slots)
                        ],
                    }
                ],
            }
        ],
        "shift_existing_attestation_load_ids": [],
    }


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
    }


def _run_scenario_worker(test_app: FastAPI) -> None:
    app_state = cast(Any, test_app.state)
    worker = ScenarioWorker(
        worker_id="scenario-api-test-worker",
        session_factory=app_state.session_factory,
        jobs=JobService(app_state.session_factory),
        artifacts=ArtifactService(app_state.session_factory, app_state.object_store),
        admitted_tariffs=app_state.admitted_tariffs,
        environment_lock_hash=app_state.environment_lock_hash,
    )
    assert worker.run_once(now=datetime.now(UTC)) is True


@pytest.mark.anyio
async def test_authenticated_scenario_runs_complete_verified_user_path(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    unauthenticated = await client.get("/v1/profiles/missing/scenario-slots")
    assert unauthenticated.status_code == 401
    owner_id, csrf = await _register(client, "scenario_owner")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    payload = await _scenario_payload(client, profile_id)

    missing_csrf = await client.post(
        "/v1/scenarios",
        headers={"Origin": ORIGIN, "Idempotency-Key": "scenario-user-path"},
        json=payload,
    )
    assert missing_csrf.status_code == 403
    created = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "scenario-user-path"),
        json=payload,
    )
    assert created.status_code == 202, created.text
    submission = created.json()
    assert submission["job"]["repeated"] is False
    assert submission["job"]["state"] == "QUEUED"
    pending = await client.get(f"/v1/scenarios/{submission['scenario_id']}")
    assert pending.status_code == 409
    assert pending.json()["code"] == "SCENARIO_RESULT_INCOMPLETE"

    _run_scenario_worker(test_app)

    completed = await client.get(f"/v1/jobs/{submission['job']['job_id']}")
    assert completed.status_code == 200
    assert completed.json()["state"] == "SUCCEEDED"
    assert completed.json()["terminal_result_type"] == "SCENARIO"
    fetched = await client.get(f"/v1/scenarios/{submission['scenario_id']}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["state"] == "SUCCEEDED"
    assert body["result"]["calculation_time_mode"] == "HISTORICAL_REPLAY"
    assert body["result"]["historical_addition_label"] == "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"
    assert body["result"]["exact"]["search_status"] == "OPTIMAL"
    assert body["result"]["exact"]["selected"]["verification"]["status"] == "VALID"
    assert body["result"]["heuristic"]["bill_optimality_claim"] is False
    assert body["result"]["manifest"]["solver_name"] == "OR-Tools CP-SAT"
    assert (
        body["result"]["manifest"]["selected_verification_sha256"]
        == (body["result"]["exact"]["selected"]["verification"]["verification_sha256"])
    )

    repeated = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "scenario-user-path"),
        json=payload,
    )
    assert repeated.status_code == 202
    assert repeated.json()["job"]["repeated"] is True
    assert repeated.json()["scenario_id"] == body["scenario_id"]
    result_resource = await client.get(f"/v1/results/{completed.json()['terminal_result_id']}")
    assert result_resource.status_code == 200
    assert result_resource.json()["result"] == body["result"]
    cancelled = await client.post(
        f"/v1/scenarios/{body['scenario_id']}/cancel",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert cancelled.status_code == 409
    assert cancelled.json()["code"] == "SCENARIO_ALREADY_TERMINAL"

    app_state = cast(Any, test_app.state)
    with app_state.session_factory() as database:
        assert database.scalar(select(func.count()).select_from(ScenarioRecord)) == 1
        assert database.scalar(select(func.count()).select_from(ScenarioResultRecord)) == 1
        assert database.scalar(select(func.count()).select_from(ScenarioLoadRecord)) == 1
        assert (
            database.scalar(select(func.count()).select_from(ScenarioReferenceScheduleRecord)) == 1
        )
        assert database.scalar(select(func.count()).select_from(CalculationManifestRecord)) == 1
        job = database.get(JobRecord, submission["job"]["job_id"])
        assert job is not None and job.kind == "SCENARIO" and job.state == "SUCCEEDED"


@pytest.mark.anyio
async def test_invalid_reference_cap_and_attestation_fail_before_job_creation(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    owner_id, csrf = await _register(client, "scenario_invalid_owner")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    payload = await _scenario_payload(client, profile_id)
    constraints = cast(dict[str, object], payload["electrical_constraints"])
    constraints["flexible_load_aggregate_cap_w"] = 500

    invalid_cap = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "scenario-invalid-cap"),
        json=payload,
    )
    assert invalid_cap.status_code == 422
    assert invalid_cap.json()["code"] == "REFERENCE_FLEXIBLE_LOAD_CAP_EXCEEDED"
    assert invalid_cap.json()["witness"]["slot_index"] == 137

    constraints["flexible_load_aggregate_cap_w"] = 2_000
    load = cast(dict[str, object], cast(list[object], payload["loads"])[0])
    load["mode"] = "SHIFT_EXISTING"
    missing_attestation = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "scenario-missing-attestation"),
        json=payload,
    )
    assert missing_attestation.status_code == 422
    assert missing_attestation.json()["code"] == "SHIFT_EXISTING_ATTESTATION_MISMATCH"
    assert missing_attestation.json()["witness"]["missing"] == [
        "00000000-0000-0000-0000-000000000001"
    ]

    app_state = cast(Any, test_app.state)
    admitted = app_state.admitted_tariffs["pge-etoud-2026-07"]
    reports = admitted.compilation.reports.model_copy(
        update={"solver_lowering_unsupported_reasons": ("SEEDED_UNSUPPORTED_OPERATOR",)}
    )
    app_state.admitted_tariffs["pge-etoud-2026-07"] = admitted.model_copy(
        update={"compilation": admitted.compilation.model_copy(update={"reports": reports})}
    )
    unsupported = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "scenario-unsupported-tariff"),
        json=payload,
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "TARIFF_OPTIMIZATION_UNAVAILABLE"
    assert unsupported.json()["witness"]["reasons"] == ["SEEDED_UNSUPPORTED_OPERATOR"]

    with app_state.session_factory() as database:
        assert database.scalar(select(func.count()).select_from(JobRecord)) == 0
        assert database.scalar(select(func.count()).select_from(ScenarioRecord)) == 0


@pytest.mark.anyio
async def test_scenario_profile_and_result_resources_are_owner_scoped(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    owner_id, csrf = await _register(client, "scenario_first_owner")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    payload = await _scenario_payload(client, profile_id)
    created = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "scenario-owner-path"),
        json=payload,
    )
    assert created.status_code == 202, created.text
    _run_scenario_worker(test_app)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        await _register(other, "scenario_second_owner")
        denied_slots = await other.get(f"/v1/profiles/{profile_id}/scenario-slots")
        denied_result = await other.get(f"/v1/scenarios/{created.json()['scenario_id']}")
        assert denied_slots.status_code == 404
        assert denied_result.status_code == 404


@pytest.mark.anyio
async def test_best_found_status_is_successful_but_never_labeled_optimal(
    client: httpx.AsyncClient,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, csrf = await _register(client, "scenario_best_found_owner")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    payload = await _scenario_payload(client, profile_id)
    original = cast(
        Callable[..., ExactOptimizationResult],
        optimize_exact,
    )

    def force_best_found(*args: object, **kwargs: object) -> ExactOptimizationResult:
        result = original(*args, **kwargs)
        first_stage = result.stage_records[0].model_copy(
            update={"status": "FEASIBLE", "fixed_optimum": None}
        )
        selected_cost = result.selected.selected.record.objective.supported_cost_cents
        return replace(
            result,
            search_status="BEST_FOUND",
            stage_records=(first_stage,),
            highest_objective_stage_proved_optimal=0,
            first_open_stage=1,
            best_supported_cost_bound=float(selected_cost - 1),
            absolute_cost_gap_cents=1.0,
            relative_cost_gap=1.0 / max(1, abs(selected_cost)),
        )

    monkeypatch.setattr(
        "ratereplay_worker.scenario_worker.optimize_exact",
        force_best_found,
    )
    response = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "scenario-best-found"),
        json=payload,
    )

    assert response.status_code == 202, response.text
    _run_scenario_worker(test_app)
    completed = await client.get(f"/v1/jobs/{response.json()['job']['job_id']}")
    assert completed.json()["state"] == "SUCCEEDED"
    fetched = await client.get(f"/v1/scenarios/{response.json()['scenario_id']}")
    result = fetched.json()["result"]
    assert result["exact"]["search_status"] == "BEST_FOUND"
    assert result["exact"]["first_open_stage"] == 1
    assert result["exact"]["highest_objective_stage_proved_optimal"] == 0
    assert result["exact"]["absolute_cost_gap_cents"] == 1.0
    assert result["manifest"]["warning_codes"] == ["EXACT_BEST_FOUND_OPEN_BOUND"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "solver_status",
    [
        "UNKNOWN",
        "MODEL_INVALID",
        "MODEL_CONTRACT_VIOLATION",
        "UNVERIFIED_INCUMBENT",
    ],
)
async def test_unsuccessful_solver_statuses_remain_distinct_and_unpublished(
    client: httpx.AsyncClient,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    solver_status: ExactSearchStatus,
) -> None:
    owner_id, csrf = await _register(client, f"scenario_status_{solver_status.lower()}")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    payload = await _scenario_payload(client, profile_id)
    original = cast(
        Callable[..., ExactOptimizationResult],
        optimize_exact,
    )

    def forced_status(*args: object, **kwargs: object) -> ExactOptimizationResult:
        result = original(*args, **kwargs)
        return replace(result, search_status=solver_status)

    monkeypatch.setattr(
        "ratereplay_worker.scenario_worker.optimize_exact",
        forced_status,
    )
    response = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, f"scenario-status-{solver_status.lower()}"),
        json=payload,
    )

    assert response.status_code == 202
    _run_scenario_worker(test_app)
    completed = await client.get(f"/v1/jobs/{response.json()['job']['job_id']}")
    assert completed.json()["state"] == "FAILED"
    assert completed.json()["failure_code"] == f"EXACT_SOLVER_{solver_status}"
    app_state = cast(Any, test_app.state)
    with app_state.session_factory() as database:
        scenario = database.get(ScenarioRecord, response.json()["scenario_id"])
        assert scenario is not None and scenario.state == "FAILED"
        assert database.scalar(select(func.count()).select_from(ScenarioResultRecord)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("tampered_request", ["{}", "[]"])
async def test_scenario_worker_rejects_tampered_durable_request(
    client: httpx.AsyncClient,
    test_app: FastAPI,
    tampered_request: str,
) -> None:
    owner_id, csrf = await _register(client, "tampered_scenario_owner")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    submitted = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "tampered-scenario-one"),
        json=await _scenario_payload(client, profile_id),
    )
    assert submitted.status_code == 202
    state = cast(Any, test_app.state)
    job_id = submitted.json()["job"]["job_id"]
    with state.session_factory.begin() as database:
        job = database.get(JobRecord, job_id)
        assert job is not None
        job.request_json = tampered_request

    _run_scenario_worker(test_app)

    completed = await client.get(f"/v1/jobs/{job_id}")
    assert completed.json()["state"] == "FAILED"
    assert completed.json()["failure_code"] == "SCENARIO_REQUEST_INVALID"
    with state.session_factory() as database:
        scenario = database.get(ScenarioRecord, submitted.json()["scenario_id"])
        assert scenario is not None and scenario.state == "FAILED"
        assert database.scalar(select(func.count()).select_from(ScenarioResultRecord)) == 0


@pytest.mark.anyio
async def test_scenario_finalizer_loses_fence_after_account_generation_change(
    client: httpx.AsyncClient,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id, csrf = await _register(client, "fenced_scenario_owner")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    submitted = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "fenced-scenario-one"),
        json=await _scenario_payload(client, profile_id),
    )
    assert submitted.status_code == 202
    state = cast(Any, test_app.state)
    original = cast(
        Callable[..., ExactOptimizationResult],
        optimize_exact,
    )

    def fence_during_calculation(*args: Any, **kwargs: Any) -> ExactOptimizationResult:
        with state.session_factory.begin() as database:
            owner = database.get(UserRecord, owner_id)
            assert owner is not None
            owner.lifecycle_generation += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        "ratereplay_worker.scenario_worker.optimize_exact",
        fence_during_calculation,
    )

    _run_scenario_worker(test_app)

    job_id = submitted.json()["job"]["job_id"]
    completed = await client.get(f"/v1/jobs/{job_id}")
    assert completed.json()["state"] == "CANCELLED"
    assert completed.json()["failure_code"] == "SCOPE_FENCED"
    with state.session_factory() as database:
        scenario = database.get(ScenarioRecord, submitted.json()["scenario_id"])
        assert scenario is not None and scenario.state == "CANCELLED"
        assert database.scalar(select(func.count()).select_from(ScenarioResultRecord)) == 0


@pytest.mark.anyio
async def test_scenario_can_be_cancelled_before_worker_execution(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    owner_id, csrf = await _register(client, "cancelled_scenario_owner")
    profile_id = _seed_hourly_july_profile(test_app, owner_id)
    submitted = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "cancelled-scenario-one"),
        json=await _scenario_payload(client, profile_id),
    )
    assert submitted.status_code == 202
    cancelled = await client.post(
        f"/v1/scenarios/{submitted.json()['scenario_id']}/cancel",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert cancelled.status_code == 202
    state = cast(Any, test_app.state)
    worker = ScenarioWorker(
        worker_id="cancelled-scenario-test-worker",
        session_factory=state.session_factory,
        jobs=JobService(state.session_factory),
        artifacts=ArtifactService(state.session_factory, state.object_store),
        admitted_tariffs=state.admitted_tariffs,
        environment_lock_hash=state.environment_lock_hash,
    )
    assert worker.run_once(now=datetime.now(UTC)) is False
    completed = await client.get(f"/v1/jobs/{submitted.json()['job']['job_id']}")
    assert completed.json()["state"] == "CANCELLED"
    with state.session_factory() as database:
        scenario = database.get(ScenarioRecord, submitted.json()["scenario_id"])
        assert scenario is not None and scenario.state == "CANCELLED"
        assert database.scalar(select(func.count()).select_from(ScenarioResultRecord)) == 0
