from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from ratereplay_ingestion.simulated import load_locked_simulated_profile
from ratereplay_optimizer.results import build_scenario_result
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import optimize_exact, optimize_off_peak_heuristic
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
    ScenarioResultRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.scenarios import ScenarioService
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.hashing import canonical_content_sha256
from sqlalchemy import delete, func, select

from benchmarks.scripts.m4_performance import _facts, _scenario

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.postgres
def test_migrated_postgres_publishes_one_owner_scoped_verified_scenario(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    owner_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=owner_id,
                username_canonical=f"scenario_{secrets.token_hex(5)}",
                password_hash="test-only",
                created_at=now,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
    imports = ImportService(sessions, FilesystemObjectStore(tmp_path / "objects"))
    installed = imports.install_simulated_profile(
        owner_user_id=owner_id,
        idempotency_key="postgres-simulated-profile",
        artifact=load_locked_simulated_profile(ROOT),
        now=now,
    )
    workload = cast(
        dict[str, Any],
        json.loads((ROOT / "benchmarks/workloads/m4-july-optimization-v2.json").read_bytes()),
    )
    scenario = _scenario(workload, 1)
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()
    bundle = compile_tariff(
        ROOT,
        ROOT / "tariffs/definitions/pge-etoud-2026-07.json",
    )
    exact = optimize_exact(validated, bundle, account, dated_facts=dated)
    heuristic = optimize_off_peak_heuristic(
        validated,
        bundle,
        account,
        dated_facts=dated,
    )
    result = build_scenario_result(
        validated,
        bundle,
        account,
        dated,
        exact,
        heuristic,
    )
    operation_hash = canonical_content_sha256(
        b"RateReplay.PostgresScenarioIntegration.v1",
        scenario.model_dump(mode="json"),
    )
    scenarios = ScenarioService(sessions)
    stored = scenarios.publish(
        owner_user_id=owner_id,
        profile_version_id=installed.profile.id,
        idempotency_key="postgres-scenario-request",
        operation_request_hash=operation_hash,
        validated=validated,
        result=result,
        now=now,
    )
    repeated = scenarios.publish(
        owner_user_id=owner_id,
        profile_version_id=installed.profile.id,
        idempotency_key="postgres-scenario-request",
        operation_request_hash=operation_hash,
        validated=validated,
        result=result,
        now=now,
    )
    semantic_reuse = scenarios.publish(
        owner_user_id=owner_id,
        profile_version_id=installed.profile.id,
        idempotency_key="postgres-scenario-semantic-reuse",
        operation_request_hash=operation_hash,
        validated=validated,
        result=result,
        now=now,
    )
    assert stored.repeated is False
    assert repeated.repeated is True
    assert semantic_reuse.repeated is True
    assert repeated.scenario_id == semantic_reuse.scenario_id == stored.scenario_id

    with sessions() as database:
        scenario_row = database.get(ScenarioRecord, stored.scenario_id)
        result_row = database.get(ScenarioResultRecord, stored.result_id)
        assert scenario_row is not None and scenario_row.state == "SUCCEEDED"
        assert result_row is not None and result_row.result_hash == result.result_sha256
        assert (
            database.scalar(
                select(func.count(ScenarioLoadRecord.id)).where(
                    ScenarioLoadRecord.scenario_id == stored.scenario_id
                )
            )
            == 1
        )
        assert (
            database.scalar(
                select(func.count(ScenarioReferenceScheduleRecord.id))
                .join(
                    ScenarioLoadRecord,
                    ScenarioLoadRecord.id == ScenarioReferenceScheduleRecord.scenario_load_id,
                )
                .where(ScenarioLoadRecord.scenario_id == stored.scenario_id)
            )
            == 1
        )
        manifest = database.scalar(
            select(CalculationManifestRecord).where(
                CalculationManifestRecord.scenario_result_id == stored.result_id
            )
        )
        assert manifest is not None
        assert manifest.calculation_hash == result.manifest.calculation_sha256

    with sessions.begin() as database:
        load_ids = select(ScenarioLoadRecord.id).where(
            ScenarioLoadRecord.scenario_id == stored.scenario_id
        )
        database.execute(
            delete(ScenarioReferenceScheduleRecord).where(
                ScenarioReferenceScheduleRecord.scenario_load_id.in_(load_ids)
            )
        )
        database.execute(
            delete(CalculationManifestRecord).where(
                CalculationManifestRecord.scenario_result_id == stored.result_id
            )
        )
        database.execute(
            delete(ScenarioResultRecord).where(ScenarioResultRecord.id == stored.result_id)
        )
        database.execute(
            delete(ScenarioLoadRecord).where(ScenarioLoadRecord.scenario_id == stored.scenario_id)
        )
        database.execute(delete(ScenarioRecord).where(ScenarioRecord.id == stored.scenario_id))
        database.execute(delete(JobAttemptRecord).where(JobAttemptRecord.job_id == stored.job_id))
        database.execute(delete(JobRecord).where(JobRecord.id == stored.job_id))
        database.execute(
            delete(OperationRequestRecord).where(OperationRequestRecord.owner_user_id == owner_id)
        )
        database.execute(
            delete(ProfileVersionRecord).where(ProfileVersionRecord.owner_user_id == owner_id)
        )
        database.execute(
            delete(ImportReadingRecord).where(
                ImportReadingRecord.import_id == installed.profile.import_id
            )
        )
        database.execute(delete(ImportRecord).where(ImportRecord.id == installed.profile.import_id))
        database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
    engine.dispose()
