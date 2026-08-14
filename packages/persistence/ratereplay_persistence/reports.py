"""Owner-scoped durable redacted-report submission and lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ratereplay_domain.semantic_identity import SemanticCalculationIdentity
from ratereplay_reports.redacted import (
    REDACTION_POLICY_VERSION,
    REPORT_CONTRACT_VERSION,
    REPORT_TEMPLATE_VERSION,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.calculations import (
    CalculationSubmissionError,
    CalculationSubmissionService,
)
from ratereplay_persistence.models import (
    JobRecord,
    ProfileVersionRecord,
    ReportExportRecord,
    ScenarioRecord,
    ScenarioResultRecord,
)

REPORT_REQUEST_SCHEMA = "report-operation-v1"


class ReportServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReportSubmission:
    job_id: str
    repeated: bool
    semantic_reuse: bool
    export_id: str | None


class ReportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        environment_lock_hash: str,
    ) -> None:
        if len(environment_lock_hash) != 64:
            raise ValueError("Environment lock hash must contain 64 characters")
        self._session_factory = session_factory
        self._submissions = CalculationSubmissionService(session_factory)
        self._environment_lock_hash = environment_lock_hash

    def submit(
        self,
        *,
        owner_user_id: str,
        scenario_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> ReportSubmission:
        with self._session_factory() as database:
            scenario = database.scalar(
                select(ScenarioRecord).where(
                    ScenarioRecord.id == scenario_id,
                    ScenarioRecord.owner_user_id == owner_user_id,
                    ScenarioRecord.lifecycle_state == "ACTIVE",
                    ScenarioRecord.state == "SUCCEEDED",
                )
            )
            result = database.scalar(
                select(ScenarioResultRecord).where(
                    ScenarioResultRecord.scenario_id == scenario_id,
                    ScenarioResultRecord.owner_user_id == owner_user_id,
                    ScenarioResultRecord.lifecycle_state == "ACTIVE",
                )
            )
            if scenario is None or result is None:
                raise ReportServiceError(
                    "SCENARIO_NOT_FOUND",
                    "Scenario is unavailable for report generation",
                )
            scenario_job = database.get(JobRecord, scenario.job_id)
            if scenario_job is None or scenario_job.state != "SUCCEEDED":
                raise ReportServiceError(
                    "SCENARIO_NOT_SUCCESSFUL",
                    "Report generation requires a successful scenario",
                )
            identity = SemanticCalculationIdentity(
                job_kind="REPORT",
                request_schema_version=REPORT_REQUEST_SCHEMA,
                calculation_contract_version=REPORT_CONTRACT_VERSION,
                environment_lock_hash=self._environment_lock_hash,
                profile_version_hash=_profile_hash(database, scenario),
                scenario_and_reference_hashes=(result.result_hash,),
                verifier_version="independent-schedule-verifier-v1",
                report_template_version=REPORT_TEMPLATE_VERSION,
            )
        try:
            submission = self._submissions.submit(
                owner_user_id=owner_user_id,
                profile_version_id=scenario.profile_version_id,
                job_kind="REPORT",
                request_schema_version=REPORT_REQUEST_SCHEMA,
                idempotency_key=idempotency_key,
                operation_payload={
                    "scenario_id": scenario_id,
                    "redaction_policy_version": REDACTION_POLICY_VERSION,
                    "report_template_version": REPORT_TEMPLATE_VERSION,
                },
                semantic_identity=identity,
                now=now,
            )
        except CalculationSubmissionError as error:
            raise ReportServiceError(error.code, str(error)) from error
        return ReportSubmission(
            job_id=submission.job_id,
            repeated=submission.repeated_operation,
            semantic_reuse=submission.semantic_reuse,
            export_id=(submission.result_id if submission.result_type == "REPORT_EXPORT" else None),
        )

    def for_scenario(
        self,
        *,
        owner_user_id: str,
        scenario_id: str,
    ) -> ReportExportRecord | None:
        with self._session_factory() as database:
            report = database.scalar(
                select(ReportExportRecord)
                .where(
                    ReportExportRecord.owner_user_id == owner_user_id,
                    ReportExportRecord.scenario_id == scenario_id,
                    ReportExportRecord.lifecycle_state == "ACTIVE",
                )
                .order_by(ReportExportRecord.created_at.desc(), ReportExportRecord.id.desc())
                .limit(1)
            )
            if report is not None:
                database.expunge(report)
            return report

    def by_id(
        self,
        *,
        owner_user_id: str,
        export_id: str,
    ) -> ReportExportRecord | None:
        with self._session_factory() as database:
            report = database.scalar(
                select(ReportExportRecord).where(
                    ReportExportRecord.id == export_id,
                    ReportExportRecord.owner_user_id == owner_user_id,
                    ReportExportRecord.lifecycle_state == "ACTIVE",
                )
            )
            if report is not None:
                database.expunge(report)
            return report


def _profile_hash(database: Session, scenario: ScenarioRecord) -> str:
    profile = database.get(ProfileVersionRecord, scenario.profile_version_id)
    if profile is None or profile.owner_user_id != scenario.owner_user_id:
        raise ReportServiceError("PROFILE_NOT_FOUND", "Scenario profile is unavailable")
    return profile.content_hash
