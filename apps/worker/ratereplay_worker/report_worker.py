"""Fenced worker for deterministic deny-by-default report exports."""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from io import BytesIO

from pydantic import ValidationError
from ratereplay_domain.telemetry import Telemetry
from ratereplay_optimizer.results import ScenarioOptimizationResult
from ratereplay_persistence.artifacts import ArtifactService, ArtifactServiceError
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
    JobRecord,
    ReportExportRecord,
    ScenarioRecord,
    ScenarioResultRecord,
)
from ratereplay_persistence.object_store import ObjectStoreError
from ratereplay_reports.redacted import (
    REDACTION_POLICY_VERSION,
    REPORT_CONTRACT_VERSION,
    REPORT_TEMPLATE_VERSION,
    ReportConstructionError,
    build_redacted_report,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


class ReportWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ReportWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        session_factory: sessionmaker[Session],
        jobs: JobService,
        artifacts: ArtifactService,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._sessions = session_factory
        self._jobs = jobs
        self._artifacts = artifacts
        self._telemetry = telemetry

    def run_once(self, *, now: datetime) -> bool:
        started = time.perf_counter()
        now = now.astimezone(UTC)
        lease = self._jobs.lease_next(
            worker_id=self._worker_id,
            now=now,
            kinds=frozenset({"REPORT"}),
        )
        if lease is None:
            return False
        if self._telemetry is not None:
            self._telemetry.record_job_lease(
                kind=lease.kind,
                job_id=lease.job_id,
                attempt_number=lease.attempt_number,
            )
        if not self._jobs.start(lease, now=now):
            return True
        try:
            self._publish(lease, now=now)
        except ReportWorkerError as error:
            self._jobs.fail(
                lease,
                code=error.code,
                retryable=error.retryable,
                now=now,
            )
            if self._telemetry is not None:
                self._telemetry.observe_report(
                    outcome="FAILED",
                    duration_seconds=time.perf_counter() - started,
                )
        except (ArtifactServiceError, ObjectStoreError):
            self._jobs.fail(
                lease,
                code="REPORT_STORAGE_UNAVAILABLE",
                retryable=True,
                now=now,
            )
            if self._telemetry is not None:
                self._telemetry.observe_report(
                    outcome="FAILED",
                    duration_seconds=time.perf_counter() - started,
                )
        else:
            if self._telemetry is not None:
                self._telemetry.observe_report(
                    outcome="SUCCEEDED",
                    duration_seconds=time.perf_counter() - started,
                )
        return True

    def _publish(self, lease: JobLease, *, now: datetime) -> None:
        with self._sessions() as database:
            job = database.get(JobRecord, lease.job_id)
            if (
                job is None
                or job.owner_user_id is None
                or job.profile_version_id is None
                or job.requested_semantic_hash is None
                or job.calculation_contract_version != REPORT_CONTRACT_VERSION
            ):
                raise ReportWorkerError(
                    "REPORT_REQUEST_INVALID",
                    "Report job does not contain a complete semantic request",
                )
            payload = _request_payload(job.request_json)
            scenario = database.get(ScenarioRecord, payload["scenario_id"])
            result = database.scalar(
                select(ScenarioResultRecord).where(
                    ScenarioResultRecord.scenario_id == payload["scenario_id"]
                )
            )
            if (
                scenario is None
                or result is None
                or scenario.owner_user_id != job.owner_user_id
                or result.owner_user_id != job.owner_user_id
                or scenario.profile_version_id != job.profile_version_id
                or result.profile_version_id != job.profile_version_id
                or scenario.lifecycle_state != "ACTIVE"
                or result.lifecycle_state != "ACTIVE"
            ):
                raise ReportWorkerError(
                    "REPORT_SCOPE_UNAVAILABLE",
                    "Report source is outside the live fenced owner scope",
                )
            try:
                scenario_result = ScenarioOptimizationResult.model_validate_json(result.result_json)
                report = build_redacted_report(scenario_result)
            except (ValidationError, ReportConstructionError) as error:
                raise ReportWorkerError(
                    "REPORT_CONSTRUCTION_FAILED",
                    "Verified scenario result cannot produce the redacted report",
                ) from error
            owner_user_id = job.owner_user_id
            semantic_hash = job.requested_semantic_hash
            scenario_id = scenario.id
            scenario_result_id = result.id
            profile_version_id = result.profile_version_id
        staged = self._artifacts.stage(
            owner_user_id=owner_user_id,
            lease=lease,
            artifact_class="REPORT",
            source=BytesIO(report.model_dump_json().encode("ascii")),
            now=now,
        )
        export_id = secrets.token_hex(16)
        export = ReportExportRecord(
            id=export_id,
            owner_user_id=owner_user_id,
            scenario_id=scenario_id,
            scenario_result_id=scenario_result_id,
            profile_version_id=profile_version_id,
            job_id=lease.job_id,
            semantic_hash=semantic_hash,
            report_hash=report.report_sha256,
            redaction_policy_version=REDACTION_POLICY_VERSION,
            report_template_version=REPORT_TEMPLATE_VERSION,
            content_json=report.model_dump_json(),
            object_key=staged.object_key,
            lifecycle_state="ACTIVE",
            lifecycle_generation=0,
            created_at=now,
        )
        finalized = self._artifacts.finalize(
            owner_user_id=owner_user_id,
            lease=lease,
            semantic_hash=semantic_hash,
            calculation_contract_version=REPORT_CONTRACT_VERSION,
            result_type="REPORT_EXPORT",
            result_id=export_id,
            artifact_registration_ids=(staged.registration_id,),
            now=now,
            publish_result=lambda database: database.add(export),
        )
        if finalized.repeated and finalized.result_id != export_id:
            self._artifacts.sweep_orphans(now=now, older_than=now)


def _request_payload(value: str) -> dict[str, str]:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ReportWorkerError(
            "REPORT_REQUEST_INVALID",
            "Report job request is not canonical JSON",
        ) from error
    expected = {
        "scenario_id",
        "redaction_policy_version",
        "report_template_version",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or not isinstance(payload["scenario_id"], str)
        or payload["redaction_policy_version"] != REDACTION_POLICY_VERSION
        or payload["report_template_version"] != REPORT_TEMPLATE_VERSION
    ):
        raise ReportWorkerError(
            "REPORT_REQUEST_INVALID",
            "Report job request schema or versions are invalid",
        )
    return payload
