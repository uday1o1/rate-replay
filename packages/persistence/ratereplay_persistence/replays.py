"""Owner-scoped immutable replay-result publication."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from ratereplay_tariffs.billing import ReplayResult
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
    UserRecord,
)

REPLAY_ROUTE: Final = "POST:/v1/replays"
REPLAY_REQUEST_SCHEMA: Final = "replay-operation-v1"
IDEMPOTENCY_RETENTION: Final = timedelta(hours=24)


class ReplayServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredReplay:
    replay_id: str
    job_id: str
    repeated: bool


class ReplayService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def publish(
        self,
        *,
        owner_user_id: str,
        profile_version_id: str,
        idempotency_key: str,
        operation_request_hash: str,
        result: ReplayResult,
        now: datetime,
    ) -> StoredReplay:
        if not 8 <= len(idempotency_key) <= 128:
            raise ReplayServiceError(
                "INVALID_IDEMPOTENCY_KEY", "Idempotency key must contain 8 to 128 characters"
            )
        now = now.astimezone(UTC)
        with self._session_factory() as database:
            existing_request = database.scalar(
                select(OperationRequestRecord).where(
                    OperationRequestRecord.owner_user_id == owner_user_id,
                    OperationRequestRecord.route_id == REPLAY_ROUTE,
                    OperationRequestRecord.idempotency_key == idempotency_key,
                )
            )
            if existing_request is not None:
                return self._repeat_or_conflict(database, existing_request, operation_request_hash)
            user = database.get(UserRecord, owner_user_id)
            profile = database.get(ProfileVersionRecord, profile_version_id)
            if user is None or user.lifecycle_state != "ACTIVE":
                raise ReplayServiceError("OWNER_NOT_ACTIVE", "Account cannot create replays")
            if (
                profile is None
                or profile.owner_user_id != owner_user_id
                or profile.lifecycle_state != "ACTIVE"
            ):
                raise ReplayServiceError("PROFILE_NOT_FOUND", "Profile is unavailable")
            imported = database.get(ImportRecord, profile.import_id)
            if imported is None or imported.lifecycle_state != "ACTIVE":
                raise ReplayServiceError("PROFILE_NOT_FOUND", "Profile scope is unavailable")
            prior_result = database.scalar(
                select(ReplayResultRecord).where(
                    ReplayResultRecord.owner_user_id == owner_user_id,
                    ReplayResultRecord.semantic_hash == result.manifest.calculation_sha256,
                )
            )
            if prior_result is not None:
                database.add(
                    self._operation_record(
                        owner_user_id=owner_user_id,
                        idempotency_key=idempotency_key,
                        operation_request_hash=operation_request_hash,
                        replay_id=prior_result.id,
                        now=now,
                    )
                )
                try:
                    database.commit()
                except IntegrityError as error:
                    database.rollback()
                    existing_request = database.scalar(
                        select(OperationRequestRecord).where(
                            OperationRequestRecord.owner_user_id == owner_user_id,
                            OperationRequestRecord.route_id == REPLAY_ROUTE,
                            OperationRequestRecord.idempotency_key == idempotency_key,
                        )
                    )
                    if existing_request is None:
                        raise ReplayServiceError(
                            "REPLAY_PUBLICATION_CONFLICT", "Replay publication conflicted"
                        ) from error
                    return self._repeat_or_conflict(
                        database, existing_request, operation_request_hash
                    )
                return StoredReplay(prior_result.id, prior_result.job_id, True)

            replay_id = secrets.token_hex(16)
            job_id = secrets.token_hex(16)
            job = JobRecord(
                id=job_id,
                owner_user_id=owner_user_id,
                kind="REPLAY",
                request_schema_version=REPLAY_REQUEST_SCHEMA,
                request_hash=operation_request_hash,
                scope_mode="ACTIVE_SCOPE",
                import_id=profile.import_id,
                captured_account_generation=user.lifecycle_generation,
                captured_import_generation=imported.lifecycle_generation,
                state="SUCCEEDED",
                attempt_count=1,
                max_attempts=1,
                fencing_generation=1,
                lease_owner="inline-reference-evaluator",
                lease_acquired_at=now,
                lease_expires_at=now,
                heartbeat_at=now,
                not_before=now,
                cancel_requested=False,
                created_at=now,
                completed_at=now,
            )
            attempt = JobAttemptRecord(
                id=secrets.token_hex(16),
                job_id=job_id,
                attempt_number=1,
                fencing_generation=1,
                worker_id="inline-reference-evaluator",
                state="SUCCEEDED",
                leased_at=now,
                lease_expires_at=now,
                completed_at=now,
            )
            replay = ReplayResultRecord(
                id=replay_id,
                owner_user_id=owner_user_id,
                profile_version_id=profile_version_id,
                job_id=job_id,
                tariff_version_id=result.manifest.tariff_version_id,
                operation_request_hash=operation_request_hash,
                semantic_hash=result.manifest.calculation_sha256,
                result_hash=result.result_sha256,
                result_json=result.model_dump_json(),
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
            manifest = CalculationManifestRecord(
                id=secrets.token_hex(16),
                replay_id=replay_id,
                calculation_hash=result.manifest.calculation_sha256,
                manifest_json=result.manifest.model_dump_json(),
                created_at=now,
            )
            operation = self._operation_record(
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
                operation_request_hash=operation_request_hash,
                replay_id=replay_id,
                now=now,
            )
            try:
                database.add(job)
                database.flush()
                database.add_all(
                    [
                        attempt,
                        replay,
                    ]
                )
                database.flush()
                database.add_all(
                    [
                        manifest,
                        operation,
                    ]
                )
                database.commit()
            except IntegrityError as error:
                database.rollback()
                existing_request = database.scalar(
                    select(OperationRequestRecord).where(
                        OperationRequestRecord.owner_user_id == owner_user_id,
                        OperationRequestRecord.route_id == REPLAY_ROUTE,
                        OperationRequestRecord.idempotency_key == idempotency_key,
                    )
                )
                if existing_request is None:
                    raise ReplayServiceError(
                        "REPLAY_PUBLICATION_CONFLICT", "Replay publication conflicted"
                    ) from error
                return self._repeat_or_conflict(database, existing_request, operation_request_hash)
            return StoredReplay(replay_id, job_id, False)

    def _operation_record(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        operation_request_hash: str,
        replay_id: str,
        now: datetime,
    ) -> OperationRequestRecord:
        return OperationRequestRecord(
            id=secrets.token_hex(16),
            owner_user_id=owner_user_id,
            route_id=REPLAY_ROUTE,
            idempotency_key=idempotency_key,
            request_schema_version=REPLAY_REQUEST_SCHEMA,
            canonical_payload_hash=operation_request_hash,
            operation_id=replay_id,
            created_at=now,
            expires_at=now + IDEMPOTENCY_RETENTION,
        )

    def _repeat_or_conflict(
        self,
        database: Session,
        operation: OperationRequestRecord,
        operation_request_hash: str,
    ) -> StoredReplay:
        if operation.canonical_payload_hash != operation_request_hash:
            raise ReplayServiceError(
                "IDEMPOTENCY_KEY_REUSED", "Idempotency key is bound to another replay request"
            )
        replay = database.get(ReplayResultRecord, operation.operation_id)
        if replay is None:
            raise ReplayServiceError("OPERATION_INCOMPLETE", "Replay operation is incomplete")
        return StoredReplay(replay.id, replay.job_id, True)
