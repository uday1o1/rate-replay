from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from ratereplay_ingestion.simulated import load_locked_simulated_profile
from ratereplay_optimizer.results import ScenarioOptimizationResult
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import default_solver_configuration
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    JobResultClaimRecord,
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
from ratereplay_tariffs.admission import load_all_admitted_tariffs
from ratereplay_worker.scenario_worker import ScenarioWorker
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
    tariff = next(
        item
        for item in load_all_admitted_tariffs(ROOT)
        if item.lock.tariff_version_id == scenario.tariff_version_id
    )
    configuration = default_solver_configuration(max_deterministic_time_per_stage=5.0)
    attestation_ids = tuple(
        sorted(str(load.load_id) for load in scenario.loads if load.mode == "SHIFT_EXISTING")
    )
    scenarios = ScenarioService(sessions)
    stored = scenarios.submit(
        owner_user_id=owner_id,
        profile_version_id=installed.profile.id,
        idempotency_key="postgres-scenario-request",
        tariff=tariff,
        account_facts=account,
        dated_facts=dated,
        validated=validated,
        attestation_load_ids=attestation_ids,
        solver_configuration=configuration,
        environment_lock_hash="e" * 64,
        now=now,
    )
    repeated = scenarios.submit(
        owner_user_id=owner_id,
        profile_version_id=installed.profile.id,
        idempotency_key="postgres-scenario-request",
        tariff=tariff,
        account_facts=account,
        dated_facts=dated,
        validated=validated,
        attestation_load_ids=attestation_ids,
        solver_configuration=configuration,
        environment_lock_hash="e" * 64,
        now=now,
    )
    assert stored.calculation.repeated_operation is False
    assert repeated.calculation.repeated_operation is True
    assert repeated.scenario_id == stored.scenario_id
    worker = ScenarioWorker(
        worker_id="postgres-scenario-worker",
        session_factory=sessions,
        jobs=JobService(sessions),
        artifacts=ArtifactService(sessions, FilesystemObjectStore(tmp_path / "artifacts")),
        admitted_tariffs={tariff.lock.tariff_version_id: tariff},
        environment_lock_hash="e" * 64,
    )
    assert worker.run_once(now=now)
    semantic_reuse = scenarios.submit(
        owner_user_id=owner_id,
        profile_version_id=installed.profile.id,
        idempotency_key="postgres-scenario-semantic-reuse",
        tariff=tariff,
        account_facts=account,
        dated_facts=dated,
        validated=validated,
        attestation_load_ids=attestation_ids,
        solver_configuration=configuration,
        environment_lock_hash="e" * 64,
        now=now,
    )
    assert semantic_reuse.calculation.semantic_reuse is True
    assert repeated.scenario_id == semantic_reuse.scenario_id == stored.scenario_id

    with sessions() as database:
        scenario_row = database.get(ScenarioRecord, stored.scenario_id)
        assert scenario_row is not None and scenario_row.state == "SUCCEEDED"
        job = database.get(JobRecord, stored.job_id)
        assert job is not None and job.state == "SUCCEEDED"
        assert job.terminal_result_id is not None
        result_id = job.terminal_result_id
        result_row = database.get(ScenarioResultRecord, result_id)
        assert result_row is not None
        result = ScenarioOptimizationResult.model_validate_json(result_row.result_json)
        assert result_row.result_hash == result.result_sha256
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
                CalculationManifestRecord.scenario_result_id == result_id
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
                CalculationManifestRecord.scenario_result_id == result_id
            )
        )
        database.execute(delete(ScenarioResultRecord).where(ScenarioResultRecord.id == result_id))
        database.execute(
            delete(ScenarioLoadRecord).where(ScenarioLoadRecord.scenario_id == stored.scenario_id)
        )
        database.execute(delete(ScenarioRecord).where(ScenarioRecord.id == stored.scenario_id))
        database.execute(
            delete(JobResultClaimRecord).where(
                JobResultClaimRecord.accepted_job_id == stored.job_id
            )
        )
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
