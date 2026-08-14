"""Queued calculation submission with separate operation and semantic identities."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final

from ratereplay_domain.semantic_identity import SemanticCalculationIdentity
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.models import (
    ImportRecord,
    JobRecord,
    JobResultClaimRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    UserRecord,
)

CALCULATION_ROUTES: Final = MappingProxyType(
    {
        "REPLAY": ("POST:/v1/replays", "replay-operation-v1"),
        "COMPARISON": ("POST:/v1/comparisons", "comparison-operation-v1"),
        "SCENARIO": ("POST:/v1/scenarios", "scenario-operation-v1"),
        "REPORT": ("POST:/v1/reports/{scenario_id}/exports", "report-operation-v1"),
    }
)
IDEMPOTENCY_RETENTION: Final = timedelta(hours=24)


class CalculationSubmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CalculationSubmission:
    job_id: str
    operation_request_hash: str
    semantic_hash: str
    repeated_operation: bool
    semantic_reuse: bool
    result_type: str | None = None
    result_id: str | None = None


class CalculationSubmissionService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def submit(
        self,
        *,
        owner_user_id: str,
        profile_version_id: str,
        job_kind: str,
        request_schema_version: str,
        idempotency_key: str,
        operation_payload: Mapping[str, object],
        semantic_identity: SemanticCalculationIdentity,
        now: datetime,
    ) -> CalculationSubmission:
        if not 8 <= len(idempotency_key) <= 128:
            raise CalculationSubmissionError(
                "INVALID_IDEMPOTENCY_KEY",
                "Idempotency key must contain 8 to 128 characters",
            )
        try:
            route_id, required_schema = CALCULATION_ROUTES[job_kind]
        except KeyError as error:
            raise CalculationSubmissionError(
                "UNSUPPORTED_CALCULATION_KIND",
                "Calculation kind is not supported",
            ) from error
        if request_schema_version != required_schema:
            raise CalculationSubmissionError(
                "REQUEST_SCHEMA_MISMATCH",
                "Request schema does not match the calculation kind",
            )
        if (
            semantic_identity.job_kind != job_kind
            or semantic_identity.request_schema_version != request_schema_version
        ):
            raise CalculationSubmissionError(
                "SEMANTIC_IDENTITY_MISMATCH",
                "Semantic identity does not match the submitted calculation",
            )
        try:
            canonical_payload = json.dumps(
                operation_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise CalculationSubmissionError(
                "OPERATION_PAYLOAD_INVALID",
                "Operation payload is not canonical JSON data",
            ) from error
        operation_request_hash = hashlib.sha256(
            b"RateReplay.OperationRequest.v1\x00"
            + route_id.encode("ascii")
            + b"\x00"
            + request_schema_version.encode("ascii")
            + b"\x00"
            + canonical_payload.encode("ascii")
        ).hexdigest()
        semantic_hash = semantic_identity.sha256()
        now = now.astimezone(UTC)
        last_integrity_error: IntegrityError | None = None
        for _attempt in range(3):
            try:
                with self._session_factory.begin() as database:
                    existing = database.scalar(
                        select(OperationRequestRecord).where(
                            OperationRequestRecord.owner_user_id == owner_user_id,
                            OperationRequestRecord.route_id == route_id,
                            OperationRequestRecord.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        return _repeat_operation(
                            database,
                            existing,
                            operation_request_hash=operation_request_hash,
                        )
                    user = database.get(UserRecord, owner_user_id)
                    profile = database.get(ProfileVersionRecord, profile_version_id)
                    if user is None or user.lifecycle_state != "ACTIVE":
                        raise CalculationSubmissionError(
                            "OWNER_NOT_ACTIVE",
                            "Account cannot create calculations",
                        )
                    if (
                        profile is None
                        or profile.owner_user_id != owner_user_id
                        or profile.lifecycle_state != "ACTIVE"
                    ):
                        raise CalculationSubmissionError(
                            "PROFILE_NOT_FOUND",
                            "Profile is unavailable",
                        )
                    if semantic_identity.profile_version_hash != profile.content_hash:
                        raise CalculationSubmissionError(
                            "PROFILE_SEMANTIC_HASH_MISMATCH",
                            "Semantic identity does not name the selected profile content",
                        )
                    imported = database.get(ImportRecord, profile.import_id)
                    if imported is None or imported.lifecycle_state != "ACTIVE":
                        raise CalculationSubmissionError(
                            "PROFILE_NOT_FOUND",
                            "Profile scope is unavailable",
                        )
                    prior = database.scalar(
                        select(JobResultClaimRecord).where(
                            JobResultClaimRecord.owner_user_id == owner_user_id,
                            JobResultClaimRecord.job_kind == job_kind,
                            JobResultClaimRecord.semantic_hash == semantic_hash,
                        )
                    )
                    if prior is not None:
                        database.add(
                            _operation_record(
                                owner_user_id=owner_user_id,
                                route_id=route_id,
                                idempotency_key=idempotency_key,
                                request_schema_version=request_schema_version,
                                operation_request_hash=operation_request_hash,
                                job_id=prior.accepted_job_id,
                                now=now,
                            )
                        )
                        return CalculationSubmission(
                            job_id=prior.accepted_job_id,
                            operation_request_hash=operation_request_hash,
                            semantic_hash=semantic_hash,
                            repeated_operation=False,
                            semantic_reuse=True,
                            result_type=prior.result_type,
                            result_id=prior.result_id,
                        )
                    job_id = secrets.token_hex(16)
                    database.add(
                        JobRecord(
                            id=job_id,
                            owner_user_id=owner_user_id,
                            kind=job_kind,
                            request_schema_version=request_schema_version,
                            request_hash=operation_request_hash,
                            request_json=canonical_payload,
                            scope_mode="ACTIVE_SCOPE",
                            import_id=profile.import_id,
                            profile_version_id=profile.id,
                            captured_account_generation=user.lifecycle_generation,
                            captured_import_generation=imported.lifecycle_generation,
                            captured_profile_generation=profile.lifecycle_generation,
                            state="QUEUED",
                            attempt_count=0,
                            max_attempts=3,
                            fencing_generation=0,
                            not_before=now,
                            cancel_requested=False,
                            requested_semantic_hash=semantic_hash,
                            calculation_contract_version=(
                                semantic_identity.calculation_contract_version
                            ),
                            created_at=now,
                        )
                    )
                    database.flush()
                    database.add(
                        _operation_record(
                            owner_user_id=owner_user_id,
                            route_id=route_id,
                            idempotency_key=idempotency_key,
                            request_schema_version=request_schema_version,
                            operation_request_hash=operation_request_hash,
                            job_id=job_id,
                            now=now,
                        )
                    )
                    return CalculationSubmission(
                        job_id=job_id,
                        operation_request_hash=operation_request_hash,
                        semantic_hash=semantic_hash,
                        repeated_operation=False,
                        semantic_reuse=False,
                    )
            except IntegrityError as error:
                last_integrity_error = error
                continue
        raise CalculationSubmissionError(
            "CALCULATION_SUBMISSION_CONFLICT",
            "Calculation submission could not resolve a concurrent request",
        ) from last_integrity_error


def _operation_record(
    *,
    owner_user_id: str,
    route_id: str,
    idempotency_key: str,
    request_schema_version: str,
    operation_request_hash: str,
    job_id: str,
    now: datetime,
) -> OperationRequestRecord:
    return OperationRequestRecord(
        id=secrets.token_hex(16),
        owner_user_id=owner_user_id,
        route_id=route_id,
        idempotency_key=idempotency_key,
        request_schema_version=request_schema_version,
        canonical_payload_hash=operation_request_hash,
        operation_id=job_id,
        created_at=now,
        expires_at=now + IDEMPOTENCY_RETENTION,
    )


def _repeat_operation(
    database: Session,
    operation: OperationRequestRecord,
    *,
    operation_request_hash: str,
) -> CalculationSubmission:
    if operation.canonical_payload_hash != operation_request_hash:
        raise CalculationSubmissionError(
            "IDEMPOTENCY_KEY_REUSED",
            "Idempotency key is bound to another calculation request",
        )
    job = database.get(JobRecord, operation.operation_id)
    if job is None or job.requested_semantic_hash is None:
        raise CalculationSubmissionError(
            "OPERATION_INCOMPLETE",
            "Calculation operation is unavailable",
        )
    return CalculationSubmission(
        job_id=job.id,
        operation_request_hash=operation_request_hash,
        semantic_hash=job.requested_semantic_hash,
        repeated_operation=True,
        semantic_reuse=job.state == "SUCCEEDED",
        result_type=job.terminal_result_type,
        result_id=job.terminal_result_id,
    )
