"""Deterministic system-scope retention scheduling and database expiry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from ratereplay_tariffs.hashing import canonical_content_sha256
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.models import (
    DeletionIntentRecord,
    DeletionReceiptRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    RawObjectRecord,
    ReplayResultRecord,
    SessionRecord,
)

RETENTION_REQUEST_SCHEMA: Final = "retention-sweep-v1"
RETENTION_CONTRACT_VERSION: Final = "system-retention-v1"
RETENTION_SCHEDULER_NAME: Final = "deadline-retention-scheduler-v1"
RETENTION_INTERVAL: Final = timedelta(hours=1)
RAW_UPLOAD_TTL: Final = timedelta(hours=24)
OPERATION_RETENTION: Final = timedelta(hours=24)
ORPHAN_GRACE: Final = timedelta(minutes=5)
TERMINAL_JOB_RETENTION: Final = timedelta(days=7)


class RetentionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_schema_version: Literal["retention-sweep-v1"] = RETENTION_REQUEST_SCHEMA
    retention_contract_version: Literal["system-retention-v1"] = RETENTION_CONTRACT_VERSION
    scheduler_name: Literal["deadline-retention-scheduler-v1"] = RETENTION_SCHEDULER_NAME
    scheduled_for: datetime
    raw_upload_ttl_seconds: Literal[86400] = 86_400
    operation_retention_seconds: Literal[86400] = 86_400
    orphan_grace_seconds: Literal[300] = 300
    terminal_job_retention_seconds: Literal[604800] = 604_800


@dataclass(frozen=True, slots=True)
class RetentionSubmission:
    job_id: str
    scheduled_for: datetime
    repeated: bool


@dataclass(frozen=True, slots=True)
class DatabaseRetentionOutcome:
    expired_operations: int
    expired_sessions: int
    expired_deletion_intents: int
    expired_retention_jobs: int


class RetentionScheduler:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def schedule(self, *, now: datetime) -> RetentionSubmission:
        now = _aware(now)
        scheduled_for = now.replace(minute=0, second=0, microsecond=0)
        return self._schedule_for(scheduled_for=scheduled_for, now=now)

    def schedule_raw_expirations(self, *, now: datetime) -> tuple[RetentionSubmission, ...]:
        """Pre-schedule exact raw-object deadlines so the 24-hour TTL is a maximum."""

        now = _aware(now)
        with self._session_factory() as database:
            deadlines = tuple(
                sorted(
                    {
                        _aware(value)
                        for value in database.scalars(
                            select(RawObjectRecord.expires_at).where(
                                RawObjectRecord.state == "AVAILABLE"
                            )
                        )
                    }
                )
            )
        return tuple(self._schedule_for(scheduled_for=deadline, now=now) for deadline in deadlines)

    def _schedule_for(self, *, scheduled_for: datetime, now: datetime) -> RetentionSubmission:
        request = RetentionRequest(scheduled_for=scheduled_for)
        request_json = _canonical_request_json(request)
        request_hash = _request_hash(request)
        job_id = request_hash[:32]
        try:
            with self._session_factory.begin() as database:
                existing = database.get(JobRecord, job_id)
                if existing is not None:
                    _validate_scheduled_job(existing, request_json, request_hash)
                    return RetentionSubmission(job_id, scheduled_for, True)
                database.add(
                    JobRecord(
                        id=job_id,
                        owner_user_id=None,
                        kind="RETENTION",
                        request_schema_version=RETENTION_REQUEST_SCHEMA,
                        request_hash=request_hash,
                        request_json=request_json,
                        scope_mode="SYSTEM_SCOPE",
                        import_id=None,
                        profile_version_id=None,
                        captured_account_generation=0,
                        captured_import_generation=None,
                        captured_profile_generation=None,
                        state="QUEUED",
                        attempt_count=0,
                        max_attempts=5,
                        fencing_generation=0,
                        not_before=scheduled_for,
                        cancel_requested=False,
                        created_at=now,
                    )
                )
            return RetentionSubmission(job_id, scheduled_for, False)
        except IntegrityError as error:
            with self._session_factory() as database:
                existing = database.get(JobRecord, job_id)
                if existing is None:
                    raise RetentionError(
                        "RETENTION_SCHEDULE_CONFLICT",
                        "Retention scheduling conflicted without a canonical job",
                    ) from error
                _validate_scheduled_job(existing, request_json, request_hash)
                return RetentionSubmission(job_id, scheduled_for, True)


class DatabaseRetentionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        ledger: FilesystemDeletionLedger,
    ) -> None:
        self._session_factory = session_factory
        self._ledger = ledger

    def expire(self, *, current_job_id: str, now: datetime) -> DatabaseRetentionOutcome:
        now = _aware(now)
        return DatabaseRetentionOutcome(
            expired_operations=self._expire_operations(now=now),
            expired_sessions=self._expire_sessions(now=now),
            expired_deletion_intents=self._expire_deletion_intents(now=now),
            expired_retention_jobs=self._expire_retention_jobs(
                current_job_id=current_job_id,
                now=now,
            ),
        )

    def _expire_operations(self, *, now: datetime) -> int:
        with self._session_factory.begin() as database:
            candidates = tuple(
                database.scalars(
                    select(OperationRequestRecord).where(OperationRequestRecord.expires_at <= now)
                )
            )
            record_ids = tuple(
                operation.id
                for operation in candidates
                if _operation_is_terminal(database, operation)
            )
            if record_ids:
                database.execute(
                    delete(OperationRequestRecord).where(OperationRequestRecord.id.in_(record_ids))
                )
            return len(record_ids)

    def _expire_sessions(self, *, now: datetime) -> int:
        with self._session_factory.begin() as database:
            session_ids = tuple(
                database.scalars(
                    select(SessionRecord.id).where(
                        (SessionRecord.revoked_at.is_not(None))
                        | (SessionRecord.idle_expires_at <= now)
                        | (SessionRecord.absolute_expires_at <= now)
                    )
                )
            )
            if session_ids:
                database.execute(delete(SessionRecord).where(SessionRecord.id.in_(session_ids)))
            return len(session_ids)

    def _expire_deletion_intents(self, *, now: datetime) -> int:
        with self._session_factory() as database:
            candidates = tuple(
                database.scalars(
                    select(DeletionIntentRecord.deletion_id).where(
                        DeletionIntentRecord.state == "INTENT_CREATED",
                        DeletionIntentRecord.expires_at <= now,
                    )
                )
            )
        expired = 0
        for deletion_id in candidates:
            with self._session_factory.begin() as database:
                intent = database.scalar(
                    select(DeletionIntentRecord)
                    .where(DeletionIntentRecord.deletion_id == deletion_id)
                    .with_for_update()
                )
                if (
                    intent is None
                    or intent.state != "INTENT_CREATED"
                    or _aware(intent.expires_at) > now
                ):
                    continue
                if self._ledger.chain(deletion_id):
                    continue
                receipt = database.get(DeletionReceiptRecord, deletion_id)
                if receipt is None or receipt.status != "INTENT_CREATED":
                    raise RetentionError(
                        "DELETION_INTENT_CONTROL_MISMATCH",
                        "Expired deletion intent has inconsistent receipt state",
                    )
                database.delete(receipt)
                database.delete(intent)
                expired += 1
        return expired

    def _expire_retention_jobs(self, *, current_job_id: str, now: datetime) -> int:
        cutoff = now - TERMINAL_JOB_RETENTION
        with self._session_factory.begin() as database:
            job_ids = tuple(
                database.scalars(
                    select(JobRecord.id).where(
                        JobRecord.kind == "RETENTION",
                        JobRecord.id != current_job_id,
                        JobRecord.state.in_(("SUCCEEDED", "FAILED", "CANCELLED")),
                        JobRecord.completed_at <= cutoff,
                    )
                )
            )
            if not job_ids:
                return 0
            database.execute(delete(JobAttemptRecord).where(JobAttemptRecord.job_id.in_(job_ids)))
            database.execute(delete(JobRecord).where(JobRecord.id.in_(job_ids)))
            return len(job_ids)


def parse_retention_request(request_json: str) -> RetentionRequest:
    try:
        request = RetentionRequest.model_validate_json(request_json)
    except ValueError as error:
        raise RetentionError(
            "RETENTION_REQUEST_INVALID",
            "Retention job request failed schema validation",
        ) from error
    if request.scheduled_for.tzinfo is None or request.scheduled_for.utcoffset() != timedelta(0):
        raise RetentionError(
            "RETENTION_REQUEST_INVALID",
            "Retention schedule must be an explicit UTC instant",
        )
    return request


def validate_retention_job(job: JobRecord) -> RetentionRequest:
    request = parse_retention_request(job.request_json)
    if (
        job.kind != "RETENTION"
        or job.scope_mode != "SYSTEM_SCOPE"
        or job.owner_user_id is not None
        or job.import_id is not None
        or job.profile_version_id is not None
        or job.request_schema_version != RETENTION_REQUEST_SCHEMA
        or job.request_hash != _request_hash(request)
        or job.request_json != _canonical_request_json(request)
    ):
        raise RetentionError(
            "RETENTION_JOB_INVALID",
            "Retention job does not match the fixed system-scope contract",
        )
    return request


def _validate_scheduled_job(job: JobRecord, request_json: str, request_hash: str) -> None:
    if job.request_json != request_json or job.request_hash != request_hash:
        raise RetentionError(
            "RETENTION_SCHEDULE_CONFLICT",
            "Deterministic retention identity is bound to another request",
        )
    validate_retention_job(job)


def _canonical_request_json(request: RetentionRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _request_hash(request: RetentionRequest) -> str:
    return canonical_content_sha256(
        b"RateReplay.SystemRetentionRequest.v1",
        request.model_dump(mode="json"),
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _operation_is_terminal(database: Session, operation: OperationRequestRecord) -> bool:
    job = database.get(JobRecord, operation.operation_id)
    if job is not None:
        return job.state in {"SUCCEEDED", "FAILED", "CANCELLED"}
    if operation.route_id == "POST:/v1/imports":
        imported = database.get(ImportRecord, operation.operation_id)
        if imported is None:
            return False
        import_job = database.scalar(
            select(JobRecord)
            .where(JobRecord.import_id == imported.id, JobRecord.kind == "IMPORT")
            .order_by(JobRecord.created_at.desc())
            .limit(1)
        )
        if import_job is not None:
            return import_job.state in {"SUCCEEDED", "FAILED", "CANCELLED"}
        return imported.state in {"READY", "CONFIRMED", "FAILED", "DELETED"}
    if operation.route_id == "POST:/v1/imports/built-in-simulated-profile":
        return database.get(ProfileVersionRecord, operation.operation_id) is not None
    if operation.route_id == "POST:/v1/replays":
        return database.get(ReplayResultRecord, operation.operation_id) is not None
    return False
