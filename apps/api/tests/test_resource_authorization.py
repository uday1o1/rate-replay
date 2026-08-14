from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_persistence.models import (
    ComparisonResultRecord,
    ImportRecord,
    JobRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
    ReportExportRecord,
    ScenarioRecord,
    ScenarioResultRecord,
    UserRecord,
)

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "https://app.ratereplay.test"
PASSWORD = "correct horse battery staple"
OWNER_ID = "1" * 32
IMPORT_ID = "2" * 32
PROFILE_ID = "3" * 32
REPLAY_ID = "4" * 32
COMPARISON_ID = "5" * 32
SCENARIO_ID = "6" * 32
RESULT_ID = "7" * 32
EXPORT_ID = "8" * 32
REPLAY_JOB_ID = "9" * 32
COMPARISON_JOB_ID = "a" * 32
SCENARIO_JOB_ID = "b" * 32
REPORT_JOB_ID = "c" * 32


@dataclass(frozen=True, slots=True)
class AuthorizationCase:
    name: str
    method: Literal["GET", "POST"]
    path: str
    json: dict[str, object] | None = None
    mutation: bool = False


AUTHORIZATION_MATRIX = (
    AuthorizationCase("direct_import", "GET", f"/v1/imports/{IMPORT_ID}"),
    AuthorizationCase("direct_profile", "GET", f"/v1/profiles/{PROFILE_ID}"),
    AuthorizationCase("indirect_profile_slots", "GET", f"/v1/profiles/{PROFILE_ID}/scenario-slots"),
    AuthorizationCase("direct_replay", "GET", f"/v1/replays/{REPLAY_ID}"),
    AuthorizationCase("direct_comparison", "GET", f"/v1/comparisons/{COMPARISON_ID}"),
    AuthorizationCase("direct_scenario", "GET", f"/v1/scenarios/{SCENARIO_ID}"),
    AuthorizationCase("direct_job", "GET", f"/v1/jobs/{REPORT_JOB_ID}"),
    AuthorizationCase("direct_result", "GET", f"/v1/results/{RESULT_ID}"),
    AuthorizationCase("direct_report", "GET", f"/v1/reports/{SCENARIO_ID}"),
    AuthorizationCase("direct_export", "GET", f"/v1/report-exports/{EXPORT_ID}"),
    AuthorizationCase(
        "indirect_replay_profile",
        "POST",
        "/v1/replays",
        {
            "request_schema_version": "replay-operation-v1",
            "profile_version_id": PROFILE_ID,
            "tariff_version_id": "pge-e1-2026-07",
            "account_facts": {},
        },
        True,
    ),
    AuthorizationCase(
        "indirect_comparison_replay",
        "POST",
        "/v1/comparisons",
        {
            "request_schema_version": "comparison-operation-v1",
            "replay_id": REPLAY_ID,
            "candidate_tariff_version_ids": ["pge-e1-2026-07", "pge-etoud-2026-07"],
            "account_facts": {},
        },
        True,
    ),
    AuthorizationCase(
        "indirect_scenario_profile",
        "POST",
        "/v1/scenarios",
        {
            "request_schema_version": "scenario-operation-v1",
            "profile_version_id": PROFILE_ID,
            "tariff_version_id": "pge-etoud-2026-07",
            "account_facts": {},
            "loads": [{}],
        },
        True,
    ),
    AuthorizationCase(
        "indirect_scenario_cancel",
        "POST",
        f"/v1/scenarios/{SCENARIO_ID}/cancel",
        {},
        True,
    ),
    AuthorizationCase(
        "indirect_report_scenario",
        "POST",
        f"/v1/reports/{SCENARIO_ID}/exports",
        {},
        True,
    ),
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


def _job(job_id: str, kind: str, now: datetime) -> JobRecord:
    return JobRecord(
        id=job_id,
        owner_user_id=OWNER_ID,
        kind=kind,
        request_schema_version=f"{kind.lower()}-operation-v1",
        request_hash="d" * 64,
        request_json="{}",
        scope_mode="ACTIVE_SCOPE",
        import_id=IMPORT_ID,
        profile_version_id=PROFILE_ID,
        captured_account_generation=0,
        captured_import_generation=0,
        captured_profile_generation=0,
        state="SUCCEEDED",
        attempt_count=1,
        max_attempts=3,
        fencing_generation=1,
        not_before=now,
        cancel_requested=False,
        created_at=now,
        completed_at=now,
    )


def _seed_other_owner_graph(test_app: FastAPI) -> None:
    state = cast(Any, test_app.state)
    now = datetime.now(UTC)
    with state.session_factory.begin() as database:
        database.add(
            UserRecord(
                id=OWNER_ID,
                username_canonical="matrix_resource_owner",
                password_hash="test-only",
                created_at=now,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
        database.flush()
        database.add(
            ImportRecord(
                id=IMPORT_ID,
                owner_user_id=OWNER_ID,
                state="CONFIRMED",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                adapter="TEST_CANONICAL",
                raw_content_hash="e" * 64,
                created_at=now,
                published_at=now,
                confirmed_at=now,
                profile_version_id=PROFILE_ID,
            )
        )
        database.flush()
        database.add(
            ProfileVersionRecord(
                id=PROFILE_ID,
                owner_user_id=OWNER_ID,
                import_id=IMPORT_ID,
                content_hash="f" * 64,
                canonical_content=b"private profile",
                billing_period_start_utc_ns=0,
                billing_period_end_utc_ns=1,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=900,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
        database.flush()
        database.add_all(
            [
                _job(REPLAY_JOB_ID, "REPLAY", now),
                _job(COMPARISON_JOB_ID, "COMPARISON", now),
                _job(SCENARIO_JOB_ID, "SCENARIO", now),
                _job(REPORT_JOB_ID, "REPORT", now),
            ]
        )
        database.flush()
        database.add(
            ReplayResultRecord(
                id=REPLAY_ID,
                owner_user_id=OWNER_ID,
                profile_version_id=PROFILE_ID,
                job_id=REPLAY_JOB_ID,
                tariff_version_id="pge-e1-2026-07",
                operation_request_hash="1" * 64,
                semantic_hash="2" * 64,
                result_hash="3" * 64,
                result_json="{}",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
        database.flush()
        database.add(
            ComparisonResultRecord(
                id=COMPARISON_ID,
                owner_user_id=OWNER_ID,
                profile_version_id=PROFILE_ID,
                current_replay_id=REPLAY_ID,
                job_id=COMPARISON_JOB_ID,
                operation_request_hash="4" * 64,
                semantic_hash="5" * 64,
                result_hash="6" * 64,
                result_json="{}",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
        database.add(
            ScenarioRecord(
                id=SCENARIO_ID,
                owner_user_id=OWNER_ID,
                profile_version_id=PROFILE_ID,
                job_id=SCENARIO_JOB_ID,
                tariff_version_id="pge-etoud-2026-07",
                operation_request_hash="7" * 64,
                input_hash="8" * 64,
                input_json="{}",
                state="SUCCEEDED",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
                completed_at=now,
            )
        )
        database.flush()
        database.add(
            ScenarioResultRecord(
                id=RESULT_ID,
                owner_user_id=OWNER_ID,
                scenario_id=SCENARIO_ID,
                profile_version_id=PROFILE_ID,
                job_id=SCENARIO_JOB_ID,
                operation_request_hash="9" * 64,
                semantic_hash="a" * 64,
                result_hash="b" * 64,
                result_json="{}",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
        database.flush()
        database.add(
            ReportExportRecord(
                id=EXPORT_ID,
                owner_user_id=OWNER_ID,
                scenario_id=SCENARIO_ID,
                scenario_result_id=RESULT_ID,
                profile_version_id=PROFILE_ID,
                job_id=REPORT_JOB_ID,
                semantic_hash="c" * 64,
                report_hash="d" * 64,
                redaction_policy_version="redacted-report-policy-v1",
                report_template_version="redacted-report-template-v1",
                content_json="{}",
                object_key=f"owners/{OWNER_ID}/reports/{EXPORT_ID}",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )


@pytest.mark.anyio
async def test_generated_resource_authorization_matrix_rejects_cross_account_ids(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    registered = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": "matrix_attacker", "password": PASSWORD},
    )
    assert registered.status_code == 201
    csrf = cast(str, registered.json()["csrf_token"])
    _seed_other_owner_graph(test_app)
    observed: dict[str, int] = {}
    for case in AUTHORIZATION_MATRIX:
        headers = {}
        if case.mutation:
            headers = {
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"matrix-{case.name}",
            }
        response = await client.request(
            case.method,
            case.path,
            headers=headers,
            json=case.json,
        )
        observed[case.name] = response.status_code
        assert response.status_code == 404, (case.name, response.text)
    assert set(observed) == {case.name for case in AUTHORIZATION_MATRIX}
