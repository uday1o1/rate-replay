"""Authenticated asynchronous import, quality, confirmation, and profile routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict
from ratereplay_ingestion.normalize import ConfirmationError
from ratereplay_ingestion.simulated import LockedSimulatedProfile
from ratereplay_persistence.imports import ImportService, ImportServiceError
from ratereplay_persistence.models import (
    ImportFindingRecord,
    ImportReadingRecord,
    ImportRecord,
    JobRecord,
    ProfileVersionRecord,
)
from ratereplay_persistence.object_store import ObjectStoreError
from sqlalchemy import BigInteger, and_, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session

from ratereplay_api.auth import AuthenticatedSession, LoginRateLimiter
from ratereplay_api.auth_routes import (
    get_authenticated_session,
    get_database,
    require_csrf_session,
)
from ratereplay_api.problems import ApiProblem, problem_openapi_responses


class ImportSubmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "import-operation-v1"
    import_id: str
    job_id: str
    state_url: str
    repeated: bool


class QualityFindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: str
    field_path: str
    warning_id: str | None


class ImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "import-v1"
    import_id: str
    lifecycle_state: str
    state: str
    job_state: str
    adapter: str
    created_at: str
    reading_count: int
    interval_resolution_seconds: int | None
    coverage_start_utc_ns: int | None
    coverage_end_utc_ns: int | None
    findings: tuple[QualityFindingResponse, ...]
    failure_code: str | None
    profile_version_id: str | None


class ConfirmImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_period_start_utc_ns: int
    billing_period_end_utc_ns: int
    acknowledged_warning_ids: tuple[str, ...] = ()
    pge_service_attested: bool


class ProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "profile-v1"
    profile_version_id: str
    import_id: str
    lifecycle_state: str
    content_hash: str
    billing_period_start_utc_ns: int
    billing_period_end_utc_ns: int
    tariff_timezone: str
    interval_resolution_seconds: int
    created_at: str


class ProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "profile-list-v1"
    items: tuple[ProfileResponse, ...]
    next_cursor: str | None


class BuiltInSimulatedProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "built-in-simulated-import-v1"
    simulated: Literal[True] = True
    label: str
    source_artifact_sha256: str
    repeated: bool
    profile: ProfileResponse


def _imports(request: Request) -> ImportService:
    return cast(ImportService, request.app.state.import_service)


def _problem(error: Exception) -> ApiProblem:
    code = getattr(error, "code", "IMPORT_FAILED")
    statuses = {
        "EMPTY_FILE": 422,
        "IDEMPOTENCY_KEY_REUSED": 409,
        "IMPORT_NOT_FOUND": 404,
        "IMPORT_NOT_READY": 409,
        "INVALID_IDEMPOTENCY_KEY": 422,
        "OPERATION_CONFLICT": 409,
        "OVERSIZED_FILE": 413,
        "PGE_SERVICE_ATTESTATION_REQUIRED": 422,
        "UNSUPPORTED_ADAPTER": 422,
        "WARNING_ACKNOWLEDGEMENT_MISMATCH": 422,
        "INCOMPLETE_BILLING_PERIOD": 422,
        "INVALID_CURSOR": 422,
    }
    return ApiProblem(
        status_code=statuses.get(code, 503),
        code=code,
        message=str(error),
    )


router = APIRouter(tags=["imports"])


@router.post(
    "/v1/imports",
    response_model=ImportSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=problem_openapi_responses(401, 403, 409, 413, 422, 503),
)
async def create_import(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    file: Annotated[UploadFile, File(description="One Green Button data file")],
    adapter: Annotated[Literal["ESPI_XML", "PGE_CSV"], Form()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ImportSubmissionResponse:
    limiter: LoginRateLimiter = request.app.state.upload_limiter
    limiter.check(f"owner:{authenticated.user_id}", now=datetime.now(UTC))
    try:
        await file.seek(0)
        submission = _imports(request).submit(
            owner_user_id=authenticated.user_id,
            adapter=adapter,
            idempotency_key=idempotency_key,
            source=file.file,
            now=datetime.now(UTC),
        )
    except (ImportServiceError, ObjectStoreError) as error:
        raise _problem(error) from error
    finally:
        await file.close()
    return ImportSubmissionResponse(
        import_id=submission.import_id,
        job_id=submission.job_id,
        state_url=f"/v1/imports/{submission.import_id}",
        repeated=submission.repeated,
    )


@router.post(
    "/v1/imports/built-in-simulated-profile",
    response_model=BuiltInSimulatedProfileResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problem_openapi_responses(401, 403, 409, 422, 503),
)
def create_built_in_simulated_profile(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> BuiltInSimulatedProfileResponse:
    """Install the immutable repository-owned July profile for this account."""

    limiter: LoginRateLimiter = request.app.state.upload_limiter
    limiter.check(f"owner:{authenticated.user_id}", now=datetime.now(UTC))
    artifact = cast(
        LockedSimulatedProfile,
        request.app.state.built_in_simulated_profile,
    )
    try:
        installed = _imports(request).install_simulated_profile(
            owner_user_id=authenticated.user_id,
            idempotency_key=idempotency_key,
            artifact=artifact,
            now=datetime.now(UTC),
        )
    except ImportServiceError as error:
        raise _problem(error) from error
    return BuiltInSimulatedProfileResponse(
        label=artifact.label,
        source_artifact_sha256=artifact.artifact_sha256,
        repeated=installed.repeated,
        profile=_profile_response(installed.profile),
    )


@router.get(
    "/v1/imports/{import_id}",
    response_model=ImportResponse,
    responses=problem_openapi_responses(401, 404),
)
def get_import(
    import_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> ImportResponse:
    imported = database.scalar(
        select(ImportRecord).where(
            ImportRecord.id == import_id,
            ImportRecord.owner_user_id == authenticated.user_id,
            ImportRecord.lifecycle_state == "ACTIVE",
        )
    )
    if imported is None:
        raise ApiProblem(status_code=404, code="IMPORT_NOT_FOUND", message="Import is unavailable")
    job_state = database.scalar(select(JobRecord.state).where(JobRecord.import_id == import_id))
    findings = database.scalars(
        select(ImportFindingRecord)
        .where(ImportFindingRecord.import_id == import_id)
        .order_by(ImportFindingRecord.code, ImportFindingRecord.field_path)
    ).all()
    reading_summary = database.execute(
        select(
            func.count(ImportReadingRecord.id),
            func.min(ImportReadingRecord.start_utc_ns),
            func.max(
                ImportReadingRecord.start_utc_ns
                + sql_cast(ImportReadingRecord.duration_seconds, BigInteger) * 1_000_000_000
            ),
            func.min(ImportReadingRecord.duration_seconds),
        ).where(ImportReadingRecord.import_id == import_id)
    ).one()
    return ImportResponse(
        import_id=imported.id,
        lifecycle_state=imported.lifecycle_state,
        state=imported.state,
        job_state=job_state or "UNKNOWN",
        adapter=imported.adapter,
        created_at=_iso(imported.created_at),
        reading_count=int(reading_summary[0] or 0),
        coverage_start_utc_ns=reading_summary[1],
        coverage_end_utc_ns=reading_summary[2],
        interval_resolution_seconds=reading_summary[3],
        findings=tuple(
            QualityFindingResponse(
                code=finding.code,
                severity=finding.severity,
                field_path=finding.field_path,
                warning_id=finding.warning_id,
            )
            for finding in findings
        ),
        failure_code=imported.failure_code,
        profile_version_id=imported.profile_version_id,
    )


@router.post(
    "/v1/imports/{import_id}/confirm",
    response_model=ProfileResponse,
    responses=problem_openapi_responses(401, 403, 404, 409, 422),
)
def confirm_import(
    import_id: str,
    payload: ConfirmImportRequest,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
) -> ProfileResponse:
    try:
        profile = _imports(request).confirm(
            owner_user_id=authenticated.user_id,
            import_id=import_id,
            billing_period_start_utc_ns=payload.billing_period_start_utc_ns,
            billing_period_end_utc_ns=payload.billing_period_end_utc_ns,
            acknowledged_warning_ids=payload.acknowledged_warning_ids,
            pge_service_attested=payload.pge_service_attested,
            now=datetime.now(UTC),
        )
    except (ImportServiceError, ConfirmationError, ObjectStoreError) as error:
        raise _problem(error) from error
    return _profile_response(profile)


@router.get(
    "/v1/profiles",
    response_model=ProfileListResponse,
    responses=problem_openapi_responses(401, 422),
)
def list_profiles(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
    cursor: Annotated[str | None, Query()] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ProfileListResponse:
    query = (
        select(ProfileVersionRecord)
        .join(ImportRecord, ImportRecord.id == ProfileVersionRecord.import_id)
        .where(
            ProfileVersionRecord.owner_user_id == authenticated.user_id,
            ProfileVersionRecord.lifecycle_state == "ACTIVE",
            ImportRecord.owner_user_id == authenticated.user_id,
            ImportRecord.lifecycle_state == "ACTIVE",
        )
    )
    if cursor is not None:
        created_at, profile_id = _decode_profile_cursor(request, cursor)
        query = query.where(
            or_(
                ProfileVersionRecord.created_at < created_at,
                and_(
                    ProfileVersionRecord.created_at == created_at,
                    ProfileVersionRecord.id < profile_id,
                ),
            )
        )
    rows = database.scalars(
        query.order_by(
            ProfileVersionRecord.created_at.desc(), ProfileVersionRecord.id.desc()
        ).limit(page_size + 1)
    ).all()
    page = rows[:page_size]
    next_cursor = None
    if len(rows) > page_size:
        last = page[-1]
        next_cursor = _encode_profile_cursor(request, last.created_at, last.id)
    return ProfileListResponse(
        items=tuple(_profile_response(profile) for profile in page),
        next_cursor=next_cursor,
    )


@router.get(
    "/v1/profiles/{profile_id}",
    response_model=ProfileResponse,
    responses=problem_openapi_responses(401, 404),
)
def get_profile(
    profile_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> ProfileResponse:
    profile = database.scalar(
        select(ProfileVersionRecord)
        .join(ImportRecord, ImportRecord.id == ProfileVersionRecord.import_id)
        .where(
            ProfileVersionRecord.id == profile_id,
            ProfileVersionRecord.owner_user_id == authenticated.user_id,
            ProfileVersionRecord.lifecycle_state == "ACTIVE",
            ImportRecord.owner_user_id == authenticated.user_id,
            ImportRecord.lifecycle_state == "ACTIVE",
        )
    )
    if profile is None:
        raise ApiProblem(
            status_code=404,
            code="PROFILE_NOT_FOUND",
            message="Profile is unavailable",
        )
    return _profile_response(profile)


def _profile_response(profile: ProfileVersionRecord) -> ProfileResponse:
    return ProfileResponse(
        profile_version_id=profile.id,
        import_id=profile.import_id,
        lifecycle_state=profile.lifecycle_state,
        content_hash=profile.content_hash,
        billing_period_start_utc_ns=profile.billing_period_start_utc_ns,
        billing_period_end_utc_ns=profile.billing_period_end_utc_ns,
        tariff_timezone=profile.tariff_timezone,
        interval_resolution_seconds=profile.interval_resolution_seconds,
        created_at=_iso(profile.created_at),
    )


def _iso(value: datetime) -> str:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def _encode_profile_cursor(request: Request, created_at: datetime, profile_id: str) -> str:
    payload = json.dumps(
        {"created_at": _iso(created_at), "profile_id": profile_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(request.app.state.settings.session_key, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def _decode_profile_cursor(request: Request, cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload, signature = decoded[:-32], decoded[-32:]
        expected = hmac.new(
            request.app.state.settings.session_key, payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        values = json.loads(payload)
        created_at = datetime.fromisoformat(values["created_at"])
        profile_id = values["profile_id"]
        if not isinstance(profile_id, str) or len(profile_id) != 32:
            raise ValueError
        return created_at, profile_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ApiProblem(
            status_code=422,
            code="INVALID_CURSOR",
            message="Profile cursor is invalid.",
        ) from error
