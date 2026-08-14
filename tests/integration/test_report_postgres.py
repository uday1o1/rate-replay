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
    ObjectUploadRegistrationRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    ReportExportRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
    ScenarioResultRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.reports import ReportService
from ratereplay_persistence.scenarios import ScenarioService
from ratereplay_reports.redacted import RedactedReport
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_worker.report_worker import ReportWorker
from sqlalchemy import delete, select

from benchmarks.scripts.m4_performance import _facts, _scenario

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.postgres
def test_migrated_postgres_publishes_one_fenced_redacted_report(tmp_path: Path) -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(tmp_path / "objects")
    owner_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    report_job_id: str | None = None
    export_id: str | None = None
    installed = None
    stored = None
    try:
        with sessions.begin() as database:
            database.add(
                UserRecord(
                    id=owner_id,
                    username_canonical=f"report_{secrets.token_hex(5)}",
                    password_hash="test-only",
                    created_at=now,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                )
            )
        imports = ImportService(sessions, objects)
        installed = imports.install_simulated_profile(
            owner_user_id=owner_id,
            idempotency_key="postgres-report-profile",
            artifact=load_locked_simulated_profile(ROOT),
            now=now,
        )
        workload = cast(
            dict[str, Any],
            json.loads((ROOT / "benchmarks/workloads/m4-july-optimization-v2.json").read_bytes()),
        )
        validated = validate_and_decompose_scenario(_scenario(workload, 1))
        account, dated = _facts()
        bundle = compile_tariff(
            ROOT,
            ROOT / "tariffs/definitions/pge-etoud-2026-07.json",
        )
        result = build_scenario_result(
            validated,
            bundle,
            account,
            dated,
            optimize_exact(validated, bundle, account, dated_facts=dated),
            optimize_off_peak_heuristic(validated, bundle, account, dated_facts=dated),
        )
        operation_hash = canonical_content_sha256(
            b"RateReplay.PostgresReportIntegration.v1",
            validated.scenario.model_dump(mode="json"),
        )
        stored = ScenarioService(sessions).publish(
            owner_user_id=owner_id,
            profile_version_id=installed.profile.id,
            idempotency_key="postgres-report-scenario",
            operation_request_hash=operation_hash,
            validated=validated,
            result=result,
            now=now,
        )
        reports = ReportService(sessions, environment_lock_hash="e" * 64)
        submission = reports.submit(
            owner_user_id=owner_id,
            scenario_id=stored.scenario_id,
            idempotency_key="postgres-report-export",
            now=now,
        )
        report_job_id = submission.job_id
        artifacts = ArtifactService(sessions, objects)
        worker = ReportWorker(
            worker_id="postgres-report-worker",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=artifacts,
        )

        assert worker.run_once(now=now)
        repeated = reports.submit(
            owner_user_id=owner_id,
            scenario_id=stored.scenario_id,
            idempotency_key="postgres-report-export",
            now=now,
        )
        semantic_reuse = reports.submit(
            owner_user_id=owner_id,
            scenario_id=stored.scenario_id,
            idempotency_key="postgres-report-export-semantic-reuse",
            now=now,
        )

        assert repeated.repeated and repeated.job_id == submission.job_id
        assert semantic_reuse.semantic_reuse
        assert semantic_reuse.job_id == submission.job_id
        assert semantic_reuse.export_id is not None
        export_id = semantic_reuse.export_id
        with sessions() as database:
            job = database.get(JobRecord, submission.job_id)
            export = database.get(ReportExportRecord, export_id)
            registration = database.scalar(
                select(ObjectUploadRegistrationRecord).where(
                    ObjectUploadRegistrationRecord.job_id == submission.job_id
                )
            )
            assert job is not None and job.state == "SUCCEEDED"
            assert export is not None and export.owner_user_id == owner_id
            assert registration is not None and registration.state == "ACCEPTED"
            report = RedactedReport.model_validate_json(export.content_json)
            assert report.report_sha256 == export.report_hash
            assert objects.exists(export.object_key)
            serialized = report.model_dump_json()
            assert "slot_start_utc" not in serialized
            assert "occurrence_id" not in serialized
            assert "object_key" not in serialized
    finally:
        if installed is not None and stored is not None:
            with sessions.begin() as database:
                if report_job_id is not None:
                    database.execute(
                        delete(ReportExportRecord).where(ReportExportRecord.job_id == report_job_id)
                    )
                    database.execute(
                        delete(ObjectUploadRegistrationRecord).where(
                            ObjectUploadRegistrationRecord.job_id == report_job_id
                        )
                    )
                    database.execute(
                        delete(JobResultClaimRecord).where(
                            JobResultClaimRecord.accepted_job_id == report_job_id
                        )
                    )
                    database.execute(
                        delete(JobAttemptRecord).where(JobAttemptRecord.job_id == report_job_id)
                    )
                    database.execute(delete(JobRecord).where(JobRecord.id == report_job_id))
                database.execute(
                    delete(CalculationManifestRecord).where(
                        CalculationManifestRecord.scenario_result_id == stored.result_id
                    )
                )
                database.execute(
                    delete(ScenarioResultRecord).where(ScenarioResultRecord.id == stored.result_id)
                )
                load_ids = select(ScenarioLoadRecord.id).where(
                    ScenarioLoadRecord.scenario_id == stored.scenario_id
                )
                database.execute(
                    delete(ScenarioReferenceScheduleRecord).where(
                        ScenarioReferenceScheduleRecord.scenario_load_id.in_(load_ids)
                    )
                )
                database.execute(
                    delete(ScenarioLoadRecord).where(
                        ScenarioLoadRecord.scenario_id == stored.scenario_id
                    )
                )
                database.execute(
                    delete(ScenarioRecord).where(ScenarioRecord.id == stored.scenario_id)
                )
                database.execute(
                    delete(JobAttemptRecord).where(JobAttemptRecord.job_id == stored.job_id)
                )
                database.execute(delete(JobRecord).where(JobRecord.id == stored.job_id))
                database.execute(
                    delete(OperationRequestRecord).where(
                        OperationRequestRecord.owner_user_id == owner_id
                    )
                )
                database.execute(
                    delete(ProfileVersionRecord).where(
                        ProfileVersionRecord.owner_user_id == owner_id
                    )
                )
                database.execute(
                    delete(ImportReadingRecord).where(
                        ImportReadingRecord.import_id == installed.profile.import_id
                    )
                )
                database.execute(
                    delete(ImportRecord).where(ImportRecord.id == installed.profile.import_id)
                )
                database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
        engine.dispose()
