"""Authenticated tariff provenance and immutable historical replay routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError
from ratereplay_persistence.models import (
    ImportReadingRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
)
from ratereplay_persistence.replays import ReplayService, ReplayServiceError
from ratereplay_tariffs.admission import AdmittedTariff, TariffAdmissionLock
from ratereplay_tariffs.billing import (
    ReplayError,
    ReplayRequest,
    ReplayResult,
    UserUnsupportedLine,
)
from ratereplay_tariffs.compiled import CompilationBundle
from ratereplay_tariffs.hashing import canonical_json_bytes
from ratereplay_tariffs.schema import AccountFacts, DateRange, FrozenModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ratereplay_api.auth import AuthenticatedSession
from ratereplay_api.auth_routes import (
    get_authenticated_session,
    get_database,
    require_csrf_session,
)
from ratereplay_api.problems import ApiProblem, problem_openapi_responses
from ratereplay_api.resource_routes import JobResourceResponse, job_resource, owned_job


class TariffSummary(FrozenModel):
    tariff_version_id: str
    plan_code: str
    utility: str
    admission_status: Literal["ADMITTED"]
    admitted_service_windows: tuple[tuple[str, str], ...]
    target_account_predicate_id: str
    calculation_time_mode: Literal["HISTORICAL_REPLAY"]
    comparison_admitted: bool
    optimization_admitted: bool


class TariffListResponse(FrozenModel):
    schema_version: Literal["tariff-list-v1"] = "tariff-list-v1"
    items: tuple[TariffSummary, ...]


class TariffDetailResponse(FrozenModel):
    schema_version: Literal["tariff-detail-v1"] = "tariff-detail-v1"
    admission: TariffAdmissionLock
    compilation: CompilationBundle


class CreateReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_schema_version: Literal["replay-operation-v1"]
    profile_version_id: str
    tariff_version_id: str
    account_facts: dict[str, object]
    current_bill_total_cents: int | None = None
    user_unsupported_lines: tuple[dict[str, object], ...] = ()


class ReplayResourceResponse(FrozenModel):
    schema_version: Literal["replay-resource-v1"] = "replay-resource-v1"
    replay_id: str
    job_id: str
    owner_user_id: str
    profile_version_id: str
    tariff_version_id: str
    lifecycle_state: str
    created_at: str
    repeated: bool
    result: ReplayResult


def _admitted_tariffs(request: Request) -> dict[str, AdmittedTariff]:
    return cast(dict[str, AdmittedTariff], request.app.state.admitted_tariffs)


def _admitted_e1(request: Request) -> AdmittedTariff:
    return _admitted_tariffs(request)["pge-e1-2026-07"]


def _replays(request: Request) -> ReplayService:
    return cast(ReplayService, request.app.state.replay_service)


def _summary(admitted: AdmittedTariff) -> TariffSummary:
    return TariffSummary(
        tariff_version_id=admitted.lock.tariff_version_id,
        plan_code=admitted.lock.plan_code,
        utility="PG&E",
        admission_status=admitted.lock.admission_status,
        admitted_service_windows=admitted.lock.admitted_service_windows,
        target_account_predicate_id=admitted.lock.target_account_predicate_id,
        calculation_time_mode=admitted.lock.scope.calculation_time_mode,
        comparison_admitted=admitted.lock.scope.comparison_admitted,
        optimization_admitted=admitted.lock.scope.optimization_admitted,
    )


def _problem(error: ReplayServiceError | ReplayError) -> ApiProblem:
    statuses = {
        "IDEMPOTENCY_KEY_REUSED": 409,
        "INVALID_IDEMPOTENCY_KEY": 422,
        "OPERATION_INCOMPLETE": 409,
        "OWNER_NOT_ACTIVE": 409,
        "PROFILE_NOT_FOUND": 404,
        "REPLAY_PUBLICATION_CONFLICT": 409,
        "TARIFF_INELIGIBLE": 422,
        "TARIFF_UNKNOWN": 422,
    }
    return ApiProblem(
        status_code=statuses.get(error.code, 422),
        code=error.code,
        message=str(error),
    )


def profile_window(profile: ProfileVersionRecord) -> DateRange:
    timezone = ZoneInfo(profile.tariff_timezone)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    def local_datetime(nanoseconds: int) -> datetime:
        seconds, remainder = divmod(nanoseconds, 1_000_000_000)
        if remainder % 1_000:
            raise ReplayError(
                "NONINTEGRAL_PROFILE_MICROSECOND",
                "Profile boundary cannot be represented by the replay clock.",
            )
        return (epoch + timedelta(seconds=seconds, microseconds=remainder // 1_000)).astimezone(
            timezone
        )

    start = local_datetime(profile.billing_period_start_utc_ns)
    end = local_datetime(profile.billing_period_end_utc_ns)
    if start.time().isoformat() != "00:00:00" or end.time().isoformat() != "00:00:00":
        raise ReplayError(
            "NONLOCAL_BILLING_BOUNDARY",
            "Confirmed profile does not begin and end at local billing-day boundaries.",
        )
    return DateRange(start=start.date(), end=end.date())


def _profile_energy(database: Session, profile: ProfileVersionRecord) -> int:
    summary = database.execute(
        select(
            func.sum(ImportReadingRecord.energy_wh),
            func.min(ImportReadingRecord.start_utc_ns),
            func.max(
                ImportReadingRecord.start_utc_ns
                + ImportReadingRecord.duration_seconds * 1_000_000_000
            ),
        ).where(
            ImportReadingRecord.import_id == profile.import_id,
            ImportReadingRecord.start_utc_ns >= profile.billing_period_start_utc_ns,
            ImportReadingRecord.start_utc_ns + ImportReadingRecord.duration_seconds * 1_000_000_000
            <= profile.billing_period_end_utc_ns,
        )
    ).one()
    if (
        summary[0] is None
        or summary[1] != profile.billing_period_start_utc_ns
        or summary[2] != profile.billing_period_end_utc_ns
    ):
        raise ReplayError(
            "PROFILE_INTERVAL_COVERAGE_MISMATCH",
            "Confirmed profile intervals do not cover the stored billing period.",
        )
    return int(summary[0])


def _resource(record: ReplayResultRecord, *, repeated: bool) -> ReplayResourceResponse:
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return ReplayResourceResponse(
        replay_id=record.id,
        job_id=record.job_id,
        owner_user_id=record.owner_user_id,
        profile_version_id=record.profile_version_id,
        tariff_version_id=record.tariff_version_id,
        lifecycle_state=record.lifecycle_state,
        created_at=created_at.isoformat(),
        repeated=repeated,
        result=ReplayResult.model_validate_json(record.result_json),
    )


router = APIRouter(tags=["tariffs", "replays"])


@router.get(
    "/v1/tariffs",
    response_model=TariffListResponse,
    responses=problem_openapi_responses(401),
)
def list_tariffs(
    request: Request,
    _authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
) -> TariffListResponse:
    return TariffListResponse(
        items=tuple(
            _summary(admitted)
            for admitted in sorted(
                _admitted_tariffs(request).values(), key=lambda item: item.lock.plan_code
            )
        )
    )


@router.get(
    "/v1/tariffs/{tariff_version_id}",
    response_model=TariffDetailResponse,
    responses=problem_openapi_responses(401, 404),
)
def get_tariff(
    tariff_version_id: str,
    request: Request,
    _authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
) -> TariffDetailResponse:
    admitted = _admitted_tariffs(request).get(tariff_version_id)
    if admitted is None:
        raise ApiProblem(status_code=404, code="TARIFF_NOT_FOUND", message="Tariff is unavailable")
    return TariffDetailResponse(admission=admitted.lock, compilation=admitted.compilation)


@router.post(
    "/v1/replays",
    response_model=JobResourceResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=problem_openapi_responses(401, 403, 404, 409, 422),
)
def create_replay(
    payload: CreateReplayRequest,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    database: Annotated[Session, Depends(get_database)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JobResourceResponse:
    admitted = _admitted_tariffs(request).get(payload.tariff_version_id)
    if admitted is None:
        raise ApiProblem(status_code=404, code="TARIFF_NOT_FOUND", message="Tariff is unavailable")
    if admitted.lock.tariff_version_id != _admitted_e1(request).lock.tariff_version_id:
        raise ApiProblem(
            status_code=422,
            code="CURRENT_REPLAY_TARIFF_UNSUPPORTED",
            message="Current-bill replay is admitted only for E-1 in this workflow",
        )
    profile = database.scalar(
        select(ProfileVersionRecord).where(
            ProfileVersionRecord.id == payload.profile_version_id,
            ProfileVersionRecord.owner_user_id == authenticated.user_id,
            ProfileVersionRecord.lifecycle_state == "ACTIVE",
        )
    )
    if profile is None:
        raise ApiProblem(
            status_code=404, code="PROFILE_NOT_FOUND", message="Profile is unavailable"
        )
    try:
        try:
            account_facts = AccountFacts.model_validate_json(
                canonical_json_bytes(payload.account_facts)
            )
            unsupported_lines = tuple(
                UserUnsupportedLine.model_validate_json(canonical_json_bytes(item))
                for item in payload.user_unsupported_lines
            )
        except ValidationError as error:
            raise ReplayError(
                "REPLAY_REQUEST_INVALID",
                "Replay account facts or unsupported lines are invalid.",
            ) from error
        service_window = profile_window(profile)
        if service_window != account_facts.service_window:
            raise ReplayError(
                "PROFILE_ACCOUNT_WINDOW_MISMATCH",
                "Account facts do not describe the confirmed profile billing period.",
            )
        replay_request = ReplayRequest(
            request_version="e1-replay-request-v1",
            profile_content_sha256=profile.content_hash,
            account_facts=account_facts,
            energy_wh=_profile_energy(database, profile),
            current_bill_total_cents=payload.current_bill_total_cents,
            user_unsupported_lines=unsupported_lines,
        )
        submission = _replays(request).submit(
            owner_user_id=authenticated.user_id,
            profile_version_id=profile.id,
            idempotency_key=idempotency_key,
            tariff=admitted,
            replay_request=replay_request,
            environment_lock_hash=cast(str, request.app.state.environment_lock_hash),
            now=datetime.now(UTC),
        )
    except (ReplayError, ReplayServiceError) as error:
        raise _problem(error) from error
    return job_resource(
        owned_job(database, authenticated.user_id, submission.job_id),
        repeated=submission.repeated_operation,
    )


@router.get(
    "/v1/replays/{replay_id}",
    response_model=ReplayResourceResponse,
    responses=problem_openapi_responses(401, 404),
)
def get_replay(
    replay_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> ReplayResourceResponse:
    record = database.scalar(
        select(ReplayResultRecord).where(
            ReplayResultRecord.id == replay_id,
            ReplayResultRecord.owner_user_id == authenticated.user_id,
            ReplayResultRecord.lifecycle_state == "ACTIVE",
        )
    )
    if record is None:
        raise ApiProblem(status_code=404, code="REPLAY_NOT_FOUND", message="Replay is unavailable")
    return _resource(record, repeated=False)
