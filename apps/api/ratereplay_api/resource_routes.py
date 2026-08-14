"""Owner-filtered durable job, result, report, and export resources."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Request, status
from ratereplay_optimizer.results import ScenarioOptimizationResult
from ratereplay_persistence.models import JobRecord, ScenarioRecord, ScenarioResultRecord
from ratereplay_persistence.reports import ReportService, ReportServiceError
from ratereplay_reports.redacted import RedactedReport
from ratereplay_tariffs.schema import FrozenModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ratereplay_api.auth import AuthenticatedSession
from ratereplay_api.auth_routes import (
    get_authenticated_session,
    get_database,
    require_csrf_session,
)
from ratereplay_api.problems import ApiProblem, problem_openapi_responses


class JobResourceResponse(FrozenModel):
    schema_version: Literal["job-resource-v1"] = "job-resource-v1"
    job_id: str
    kind: str
    state: str
    operation_request_hash: str
    semantic_hash: str | None
    failure_code: str | None
    terminal_result_type: str | None
    terminal_result_id: str | None
    created_at: str
    completed_at: str | None
    repeated: bool = False


class ScenarioResultResourceResponse(FrozenModel):
    schema_version: Literal["scenario-result-resource-v1"] = "scenario-result-resource-v1"
    result_id: str
    scenario_id: str
    job_id: str
    created_at: str
    result: ScenarioOptimizationResult


class ReportResourceResponse(FrozenModel):
    schema_version: Literal["report-resource-v1"] = "report-resource-v1"
    export_id: str
    scenario_id: str
    scenario_result_id: str
    job_id: str
    created_at: str
    report: RedactedReport


def _reports(request: Request) -> ReportService:
    return cast(ReportService, request.app.state.report_service)


def _iso(value: datetime) -> str:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def job_resource(job: JobRecord, *, repeated: bool = False) -> JobResourceResponse:
    return JobResourceResponse(
        job_id=job.id,
        kind=job.kind,
        state=job.state,
        operation_request_hash=job.request_hash,
        semantic_hash=job.requested_semantic_hash,
        failure_code=job.failure_code,
        terminal_result_type=job.terminal_result_type,
        terminal_result_id=job.terminal_result_id,
        created_at=_iso(job.created_at),
        completed_at=_iso(job.completed_at) if job.completed_at is not None else None,
        repeated=repeated,
    )


def owned_job(database: Session, owner_user_id: str, job_id: str) -> JobRecord:
    job = database.scalar(
        select(JobRecord).where(
            JobRecord.id == job_id,
            JobRecord.owner_user_id == owner_user_id,
        )
    )
    if job is None:
        raise ApiProblem(status_code=404, code="JOB_NOT_FOUND", message="Job is unavailable")
    return job


def _report_resource(record: object) -> ReportResourceResponse:
    from ratereplay_persistence.models import ReportExportRecord

    if not isinstance(record, ReportExportRecord):
        raise TypeError("Report resource requires a persisted export")
    try:
        report = RedactedReport.model_validate_json(record.content_json)
    except ValueError as error:
        raise ApiProblem(
            status_code=500,
            code="REPORT_CONTENT_INVALID",
            message="Stored report failed schema validation",
        ) from error
    if report.report_sha256 != record.report_hash:
        raise ApiProblem(
            status_code=500,
            code="REPORT_CONTENT_INVALID",
            message="Stored report failed integrity validation",
        )
    return ReportResourceResponse(
        export_id=record.id,
        scenario_id=record.scenario_id,
        scenario_result_id=record.scenario_result_id,
        job_id=record.job_id,
        created_at=_iso(record.created_at),
        report=report,
    )


router = APIRouter(tags=["jobs", "reports"])


@router.get(
    "/v1/jobs/{job_id}",
    response_model=JobResourceResponse,
    responses=problem_openapi_responses(401, 404),
)
def get_job(
    job_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> JobResourceResponse:
    return job_resource(owned_job(database, authenticated.user_id, job_id))


@router.get(
    "/v1/results/{result_id}",
    response_model=ScenarioResultResourceResponse,
    responses=problem_openapi_responses(401, 404),
)
def get_result(
    result_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> ScenarioResultResourceResponse:
    result = database.scalar(
        select(ScenarioResultRecord).where(
            ScenarioResultRecord.id == result_id,
            ScenarioResultRecord.owner_user_id == authenticated.user_id,
            ScenarioResultRecord.lifecycle_state == "ACTIVE",
        )
    )
    scenario = (
        database.scalar(
            select(ScenarioRecord).where(
                ScenarioRecord.id == result.scenario_id,
                ScenarioRecord.owner_user_id == authenticated.user_id,
                ScenarioRecord.lifecycle_state == "ACTIVE",
            )
        )
        if result is not None
        else None
    )
    if result is None or scenario is None:
        raise ApiProblem(
            status_code=404,
            code="RESULT_NOT_FOUND",
            message="Result is unavailable",
        )
    return ScenarioResultResourceResponse(
        result_id=result.id,
        scenario_id=scenario.id,
        job_id=result.job_id,
        created_at=_iso(result.created_at),
        result=ScenarioOptimizationResult.model_validate_json(result.result_json),
    )


@router.post(
    "/v1/reports/{scenario_id}/exports",
    response_model=JobResourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=problem_openapi_responses(401, 403, 404, 409, 422),
)
def create_report_export(
    scenario_id: str,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    database: Annotated[Session, Depends(get_database)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JobResourceResponse:
    try:
        submission = _reports(request).submit(
            owner_user_id=authenticated.user_id,
            scenario_id=scenario_id,
            idempotency_key=idempotency_key,
            now=datetime.now(UTC),
        )
        request.state.job_id = submission.job_id
    except ReportServiceError as error:
        statuses = {
            "IDEMPOTENCY_KEY_REUSED": 409,
            "INVALID_IDEMPOTENCY_KEY": 422,
            "OPERATION_INCOMPLETE": 409,
            "OWNER_NOT_ACTIVE": 409,
            "PROFILE_NOT_FOUND": 404,
            "SCENARIO_NOT_FOUND": 404,
            "SCENARIO_NOT_SUCCESSFUL": 409,
        }
        raise ApiProblem(
            status_code=statuses.get(error.code, 422),
            code=error.code,
            message=str(error),
        ) from error
    return job_resource(
        owned_job(database, authenticated.user_id, submission.job_id),
        repeated=submission.repeated,
    )


@router.get(
    "/v1/reports/{scenario_id}",
    response_model=ReportResourceResponse,
    responses=problem_openapi_responses(401, 404, 500),
)
def get_report(
    scenario_id: str,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
) -> ReportResourceResponse:
    record = _reports(request).for_scenario(
        owner_user_id=authenticated.user_id,
        scenario_id=scenario_id,
    )
    if record is None:
        raise ApiProblem(
            status_code=404,
            code="REPORT_NOT_FOUND",
            message="Report is unavailable",
        )
    return _report_resource(record)


@router.get(
    "/v1/report-exports/{export_id}",
    response_model=ReportResourceResponse,
    responses=problem_openapi_responses(401, 404, 500),
)
def get_report_export(
    export_id: str,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
) -> ReportResourceResponse:
    record = _reports(request).by_id(
        owner_user_id=authenticated.user_id,
        export_id=export_id,
    )
    if record is None:
        raise ApiProblem(
            status_code=404,
            code="REPORT_EXPORT_NOT_FOUND",
            message="Report export is unavailable",
        )
    return _report_resource(record)
