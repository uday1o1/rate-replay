"""Authenticated immutable tariff comparison routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ratereplay_persistence.comparisons import ComparisonService, ComparisonServiceError
from ratereplay_persistence.models import (
    ComparisonResultRecord,
    ImportReadingRecord,
    JobRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
)
from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayError,
    ReplayInterval,
    ReplayResult,
    evaluate_eligibility,
)
from ratereplay_tariffs.comparison import (
    ComparisonError,
    ComparisonResult,
    load_required_component_keys,
)
from ratereplay_tariffs.hashing import canonical_json_bytes
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts, FrozenModel
from sqlalchemy import BigInteger, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session

from ratereplay_api.auth import AuthenticatedSession
from ratereplay_api.auth_routes import (
    get_authenticated_session,
    get_database,
    require_csrf_session,
)
from ratereplay_api.config import AppSettings
from ratereplay_api.problems import ApiProblem, problem_openapi_responses
from ratereplay_api.replay_routes import profile_window
from ratereplay_api.resource_routes import JobResourceResponse, job_resource, owned_job


class CreateComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_schema_version: Literal["comparison-operation-v1"]
    replay_id: str = Field(min_length=1)
    candidate_tariff_version_ids: tuple[str, ...] = Field(min_length=2, max_length=5)
    account_facts: dict[str, object]
    dated_eligibility_facts: dict[str, object] | None = None


class ComparisonResourceResponse(FrozenModel):
    schema_version: Literal["comparison-resource-v1"] = "comparison-resource-v1"
    comparison_id: str
    job_id: str
    owner_user_id: str
    profile_version_id: str
    current_replay_id: str
    lifecycle_state: str
    created_at: str
    repeated: bool
    result: ComparisonResult


def _tariffs(request: Request) -> dict[str, AdmittedTariff]:
    return cast(dict[str, AdmittedTariff], request.app.state.admitted_tariffs)


def _comparisons(request: Request) -> ComparisonService:
    return cast(ComparisonService, request.app.state.comparison_service)


def _problem(error: ComparisonError | ComparisonServiceError | ReplayError) -> ApiProblem:
    statuses = {
        "COMPARISON_PUBLICATION_CONFLICT": 409,
        "CURRENT_REPLAY_ACCOUNT_MISMATCH": 409,
        "DUPLICATE_CANDIDATE": 422,
        "IDEMPOTENCY_KEY_REUSED": 409,
        "INVALID_IDEMPOTENCY_KEY": 422,
        "OPERATION_INCOMPLETE": 409,
        "OWNER_NOT_ACTIVE": 409,
        "PROFILE_INTERVAL_COVERAGE_MISMATCH": 422,
        "PROFILE_NOT_FOUND": 404,
        "REPLAY_NOT_FOUND": 404,
    }
    return ApiProblem(
        status_code=statuses.get(error.code, 422),
        code=error.code,
        message=str(error),
    )


def _profile_intervals(
    database: Session, profile: ProfileVersionRecord
) -> tuple[ReplayInterval, ...]:
    records = tuple(
        database.scalars(
            select(ImportReadingRecord)
            .where(
                ImportReadingRecord.import_id == profile.import_id,
                ImportReadingRecord.start_utc_ns >= profile.billing_period_start_utc_ns,
                ImportReadingRecord.start_utc_ns
                + sql_cast(ImportReadingRecord.duration_seconds, BigInteger) * 1_000_000_000
                <= profile.billing_period_end_utc_ns,
            )
            .order_by(ImportReadingRecord.start_utc_ns)
        )
    )
    expected_start = profile.billing_period_start_utc_ns
    intervals: list[ReplayInterval] = []
    for record in records:
        if record.start_utc_ns != expected_start or record.flow_direction != "IMPORT":
            raise ReplayError(
                "PROFILE_INTERVAL_COVERAGE_MISMATCH",
                "Confirmed profile intervals are not complete import coverage",
            )
        try:
            interval = ReplayInterval(
                start_utc_ns=record.start_utc_ns,
                duration_seconds=record.duration_seconds,
                energy_wh=record.energy_wh,
            )
        except ValidationError as error:
            raise ReplayError(
                "PROFILE_INTERVAL_UNSUPPORTED",
                "Confirmed profile contains intervals unsupported by tariff comparison",
            ) from error
        intervals.append(interval)
        expected_start += interval.duration_seconds * 1_000_000_000
    if not intervals or expected_start != profile.billing_period_end_utc_ns:
        raise ReplayError(
            "PROFILE_INTERVAL_COVERAGE_MISMATCH",
            "Confirmed profile intervals do not cover the complete billing period",
        )
    return tuple(intervals)


def _resource(record: ComparisonResultRecord, *, repeated: bool) -> ComparisonResourceResponse:
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return ComparisonResourceResponse(
        comparison_id=record.id,
        job_id=record.job_id,
        owner_user_id=record.owner_user_id,
        profile_version_id=record.profile_version_id,
        current_replay_id=record.current_replay_id,
        lifecycle_state=record.lifecycle_state,
        created_at=created_at.isoformat(),
        repeated=repeated,
        result=ComparisonResult.model_validate_json(record.result_json),
    )


router = APIRouter(tags=["comparisons"])


@router.post(
    "/v1/comparisons",
    response_model=JobResourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=problem_openapi_responses(401, 403, 404, 409, 422),
)
def create_comparison(
    payload: CreateComparisonRequest,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    database: Annotated[Session, Depends(get_database)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JobResourceResponse:
    current_replay = database.scalar(
        select(ReplayResultRecord).where(
            ReplayResultRecord.id == payload.replay_id,
            ReplayResultRecord.owner_user_id == authenticated.user_id,
            ReplayResultRecord.lifecycle_state == "ACTIVE",
        )
    )
    if current_replay is None:
        raise ApiProblem(status_code=404, code="REPLAY_NOT_FOUND", message="Replay is unavailable")
    current_job = database.get(JobRecord, current_replay.job_id)
    if current_job is None or current_job.state != "SUCCEEDED":
        raise ApiProblem(
            status_code=409,
            code="REPLAY_NOT_SUCCESSFUL",
            message="Comparison requires a successful replay",
        )
    profile = database.scalar(
        select(ProfileVersionRecord).where(
            ProfileVersionRecord.id == current_replay.profile_version_id,
            ProfileVersionRecord.owner_user_id == authenticated.user_id,
            ProfileVersionRecord.lifecycle_state == "ACTIVE",
        )
    )
    if profile is None:
        raise ApiProblem(
            status_code=404, code="PROFILE_NOT_FOUND", message="Profile is unavailable"
        )
    candidate_ids = tuple(payload.candidate_tariff_version_ids)
    admitted_by_id = _tariffs(request)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ApiProblem(
            status_code=422,
            code="DUPLICATE_CANDIDATE",
            message="Comparison candidates must be unique",
        )
    missing_ids = tuple(sorted(set(candidate_ids) - set(admitted_by_id)))
    if missing_ids:
        raise ApiProblem(
            status_code=404,
            code="TARIFF_NOT_FOUND",
            message="One or more candidate tariffs are unavailable",
        )
    try:
        try:
            account_facts = AccountFacts.model_validate_json(
                canonical_json_bytes(payload.account_facts)
            )
            dated_facts = (
                DatedEligibilityFacts.model_validate_json(
                    canonical_json_bytes(payload.dated_eligibility_facts)
                )
                if payload.dated_eligibility_facts is not None
                else None
            )
        except ValidationError as error:
            raise ReplayError(
                "COMPARISON_REQUEST_INVALID", "Comparison eligibility facts are invalid"
            ) from error
        if profile_window(profile) != account_facts.service_window:
            raise ReplayError(
                "PROFILE_ACCOUNT_WINDOW_MISMATCH",
                "Account facts do not describe the confirmed profile billing period",
            )
        current_admitted = admitted_by_id.get(current_replay.tariff_version_id)
        if current_admitted is None:
            raise ComparisonError(
                "CURRENT_TARIFF_NOT_ADMITTED", "Current replay tariff is not admitted"
            )
        current_result = ReplayResult.model_validate_json(current_replay.result_json)
        provided_current_eligibility = evaluate_eligibility(
            current_admitted.compilation, account_facts, dated_facts
        )
        if (
            provided_current_eligibility.account_facts_sha256
            != current_result.eligibility.account_facts_sha256
        ):
            raise ComparisonError(
                "CURRENT_REPLAY_ACCOUNT_MISMATCH",
                "Comparison account facts differ from the current replay",
            )
        intervals = _profile_intervals(database, profile)
        comparison_request = IntervalReplayRequest(
            request_version="interval-replay-request-v1",
            profile_content_sha256=profile.content_hash,
            account_facts=account_facts,
            energy_wh=sum(interval.energy_wh for interval in intervals),
            intervals=intervals,
            dated_eligibility_facts=dated_facts,
        )
        sorted_candidate_ids = tuple(sorted(candidate_ids))
        required_component_keys = load_required_component_keys(
            cast(AppSettings, request.app.state.settings).repository_root
        )
        submission = _comparisons(request).submit(
            owner_user_id=authenticated.user_id,
            profile_version_id=profile.id,
            current_replay_id=current_replay.id,
            idempotency_key=idempotency_key,
            tariffs=tuple(admitted_by_id[candidate_id] for candidate_id in sorted_candidate_ids),
            comparison_request=comparison_request,
            required_component_keys=required_component_keys,
            environment_lock_hash=cast(str, request.app.state.environment_lock_hash),
            now=datetime.now(UTC),
        )
    except (ComparisonError, ComparisonServiceError, ReplayError) as error:
        raise _problem(error) from error
    return job_resource(
        owned_job(database, authenticated.user_id, submission.job_id),
        repeated=submission.repeated_operation,
    )


@router.get(
    "/v1/comparisons/{comparison_id}",
    response_model=ComparisonResourceResponse,
    responses=problem_openapi_responses(401, 404),
)
def get_comparison(
    comparison_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> ComparisonResourceResponse:
    record = database.scalar(
        select(ComparisonResultRecord).where(
            ComparisonResultRecord.id == comparison_id,
            ComparisonResultRecord.owner_user_id == authenticated.user_id,
            ComparisonResultRecord.lifecycle_state == "ACTIVE",
        )
    )
    if record is None:
        raise ApiProblem(
            status_code=404,
            code="COMPARISON_NOT_FOUND",
            message="Comparison is unavailable",
        )
    return _resource(record, repeated=False)
