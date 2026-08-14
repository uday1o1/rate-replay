"""Session-bound deletion requests and session-independent receipt polling."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from ratereplay_persistence.deletion_ledger import DeletionLedgerError
from ratereplay_persistence.deletions import (
    DeletionCoordinator,
    DeletionServiceError,
    DeletionStatus,
)

from ratereplay_api.auth import AuthenticatedSession
from ratereplay_api.auth_routes import require_csrf_session
from ratereplay_api.problems import ApiProblem, problem_openapi_responses

RECEIPT_HEADER = "X-Deletion-Receipt-Secret"


class DeletionIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "deletion-intent-v1"
    deletion_id: str
    status: str
    expires_at: str


class AccountDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deletion_id: str = Field(min_length=32, max_length=32, pattern="^[0-9a-f]{32}$")


class DeletionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "deletion-status-v1"
    deletion_id: str
    status: str
    artifact_counts: dict[str, int]
    completed_at: str | None


def get_deletion_coordinator(request: Request) -> DeletionCoordinator:
    return cast(DeletionCoordinator, request.app.state.deletion_coordinator)


def _receipt_secret(value: str | None) -> bytes:
    if value is None or len(value) != 43:
        raise ApiProblem(
            status_code=404,
            code="INVALID_DELETION_PROOF",
            message="Deletion receipt authorization failed.",
        )
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ApiProblem(
            status_code=404,
            code="INVALID_DELETION_PROOF",
            message="Deletion receipt authorization failed.",
        ) from error
    if len(decoded) != 32:
        raise ApiProblem(
            status_code=404,
            code="INVALID_DELETION_PROOF",
            message="Deletion receipt authorization failed.",
        )
    return decoded


def _problem(error: DeletionServiceError) -> ApiProblem:
    status_code = {
        "ACCOUNT_NOT_ACTIVE": 409,
        "DELETION_ALREADY_PENDING": 409,
        "IDEMPOTENCY_CONFLICT": 409,
        "INTENT_EXPIRED": 410,
        "DELETION_RECEIPT_EXPIRED": 410,
        "INVALID_DELETION_PROOF": 404,
    }.get(error.code, 503)
    safe_message = {
        404: "Deletion receipt authorization failed.",
        409: str(error),
        410: str(error),
        503: "Deletion coordination is temporarily unavailable.",
    }[status_code]
    return ApiProblem(status_code=status_code, code=error.code, message=safe_message)


def _response(status_view: DeletionStatus) -> DeletionStatusResponse:
    return DeletionStatusResponse(
        deletion_id=status_view.deletion_id,
        status=status_view.status,
        artifact_counts=status_view.artifact_counts,
        completed_at=(
            status_view.completed_at.isoformat() if status_view.completed_at is not None else None
        ),
    )


router = APIRouter(tags=["deletion"])


@router.post(
    "/v1/account/deletion-intents",
    response_model=DeletionIntentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problem_openapi_responses(404, 409, 410, 422, 503),
)
def create_deletion_intent(
    request: Request,
    response: Response,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    coordinator: Annotated[DeletionCoordinator, Depends(get_deletion_coordinator)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    receipt_header: Annotated[str | None, Header(alias=RECEIPT_HEADER)] = None,
) -> DeletionIntentResponse:
    try:
        intent = coordinator.create_intent(
            owner_user_id=authenticated.user_id,
            idempotency_key=idempotency_key,
            receipt_secret=_receipt_secret(receipt_header),
            now=request.app.state.auth_service.now,
        )
    except DeletionServiceError as error:
        raise _problem(error) from error
    response.headers["Cache-Control"] = "no-store"
    return DeletionIntentResponse(
        deletion_id=intent.deletion_id,
        status=intent.status,
        expires_at=intent.expires_at.isoformat(),
    )


@router.delete(
    "/v1/account",
    response_model=DeletionStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=problem_openapi_responses(404, 409, 410, 422, 503),
)
def delete_account(
    payload: AccountDeletionRequest,
    request: Request,
    response: Response,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    coordinator: Annotated[DeletionCoordinator, Depends(get_deletion_coordinator)],
    receipt_header: Annotated[str | None, Header(alias=RECEIPT_HEADER)] = None,
) -> DeletionStatusResponse:
    try:
        deletion_status = coordinator.authorize_and_start(
            owner_user_id=authenticated.user_id,
            deletion_id=payload.deletion_id,
            receipt_secret=_receipt_secret(receipt_header),
            now=request.app.state.auth_service.now,
        )
    except DeletionServiceError as error:
        raise _problem(error) from error
    except DeletionLedgerError as error:
        raise ApiProblem(
            status_code=503,
            code=error.code,
            message="Deletion coordination is temporarily unavailable.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return _response(deletion_status)


@router.get(
    "/v1/deletions/{deletion_id}",
    response_model=DeletionStatusResponse,
    responses=problem_openapi_responses(404, 410, 422, 503),
)
def deletion_status(
    deletion_id: str,
    request: Request,
    response: Response,
    coordinator: Annotated[DeletionCoordinator, Depends(get_deletion_coordinator)],
    receipt_header: Annotated[str | None, Header(alias=RECEIPT_HEADER)] = None,
) -> DeletionStatusResponse:
    if len(deletion_id) != 32 or any(
        character not in "0123456789abcdef" for character in deletion_id
    ):
        raise ApiProblem(
            status_code=404,
            code="INVALID_DELETION_PROOF",
            message="Deletion receipt authorization failed.",
        )
    try:
        status_view = coordinator.status(
            deletion_id=deletion_id,
            receipt_secret=_receipt_secret(receipt_header),
            now=request.app.state.auth_service.now,
        )
    except DeletionServiceError as error:
        raise _problem(error) from error
    response.headers["Cache-Control"] = "no-store"
    return _response(status_view)
