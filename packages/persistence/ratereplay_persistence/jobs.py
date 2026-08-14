"""PostgreSQL-leased job execution with attempt fencing and bounded retry."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.models import (
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    UserRecord,
)

LEASE_DURATION = timedelta(seconds=20)
RETRY_BASE = timedelta(seconds=2)


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    import_id: str
    worker_id: str
    attempt_number: int
    fencing_generation: int
    lease_expires_at: datetime


class JobService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def lease_next(self, *, worker_id: str, now: datetime) -> JobLease | None:
        now = now.astimezone(UTC)
        self.rescue_expired(now=now)
        with self._session_factory.begin() as database:
            query = (
                select(JobRecord)
                .where(
                    JobRecord.state == "QUEUED",
                    JobRecord.not_before <= now,
                    JobRecord.cancel_requested.is_(False),
                    JobRecord.kind == "IMPORT",
                    JobRecord.scope_mode == "ACTIVE_SCOPE",
                )
                .order_by(JobRecord.created_at, JobRecord.id)
                .limit(1)
            )
            if database.bind is not None and database.bind.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            job = database.scalar(query)
            if job is None:
                return None
            user = database.get(UserRecord, job.owner_user_id)
            imported = database.get(ImportRecord, job.import_id)
            if not _scope_is_active(job, user, imported):
                job.state = "CANCELLED"
                job.failure_code = "SCOPE_FENCED"
                job.completed_at = now
                return None
            if imported is None:
                return None
            if job.attempt_count >= job.max_attempts:
                job.state = "FAILED"
                job.failure_code = "ATTEMPT_BUDGET_EXHAUSTED"
                job.completed_at = now
                imported.state = "FAILED"
                imported.failure_code = job.failure_code
                return None
            job.attempt_count += 1
            job.fencing_generation += 1
            job.state = "LEASED"
            job.lease_owner = worker_id
            job.lease_acquired_at = now
            job.heartbeat_at = now
            job.lease_expires_at = now + LEASE_DURATION
            imported.state = "PROCESSING"
            database.add(
                JobAttemptRecord(
                    id=secrets.token_hex(16),
                    job_id=job.id,
                    attempt_number=job.attempt_count,
                    fencing_generation=job.fencing_generation,
                    worker_id=worker_id,
                    state="LEASED",
                    leased_at=now,
                    lease_expires_at=job.lease_expires_at,
                )
            )
            return JobLease(
                job_id=job.id,
                import_id=job.import_id,
                worker_id=worker_id,
                attempt_number=job.attempt_count,
                fencing_generation=job.fencing_generation,
                lease_expires_at=job.lease_expires_at,
            )

    def start(self, lease: JobLease, *, now: datetime) -> bool:
        return self._conditional_state(
            lease,
            expected_state="LEASED",
            next_state="RUNNING",
            now=now,
        )

    def heartbeat(self, lease: JobLease, *, now: datetime) -> bool:
        now = now.astimezone(UTC)
        with self._session_factory.begin() as database:
            job = database.get(JobRecord, lease.job_id)
            if job is None:
                return False
            user = database.get(UserRecord, job.owner_user_id)
            imported = database.get(ImportRecord, job.import_id)
            if not (
                job.state in {"LEASED", "RUNNING"}
                and _lease_matches(job, lease)
                and job.lease_expires_at is not None
                and now < _aware(job.lease_expires_at)
                and _scope_is_active(job, user, imported)
            ):
                return False
            job.heartbeat_at = now
            job.lease_expires_at = now + LEASE_DURATION
            attempt = database.scalar(
                select(JobAttemptRecord).where(
                    JobAttemptRecord.job_id == job.id,
                    JobAttemptRecord.fencing_generation == lease.fencing_generation,
                )
            )
            if attempt is not None:
                attempt.lease_expires_at = job.lease_expires_at
            return True

    def fail(
        self,
        lease: JobLease,
        *,
        code: str,
        retryable: bool,
        now: datetime,
    ) -> bool:
        now = now.astimezone(UTC)
        with self._session_factory.begin() as database:
            job = database.get(JobRecord, lease.job_id)
            if (
                job is None
                or job.state not in {"LEASED", "RUNNING"}
                or not _lease_matches(job, lease)
            ):
                return False
            imported = database.get(ImportRecord, job.import_id)
            if imported is None:
                return False
            attempt = database.scalar(
                select(JobAttemptRecord).where(
                    JobAttemptRecord.job_id == job.id,
                    JobAttemptRecord.fencing_generation == lease.fencing_generation,
                )
            )
            if attempt is not None:
                attempt.state = "FAILED"
                attempt.failure_code = code
                attempt.completed_at = now
            if retryable and job.attempt_count < job.max_attempts:
                job.state = "QUEUED"
                job.not_before = now + _retry_delay(job.id, job.attempt_count)
                imported.state = "QUEUED"
            else:
                job.state = "FAILED"
                job.failure_code = code if not retryable else "ATTEMPT_BUDGET_EXHAUSTED"
                job.completed_at = now
                imported.state = "FAILED"
                imported.failure_code = job.failure_code
            job.lease_owner = None
            job.lease_expires_at = None
            return True

    def cancel(self, *, owner_user_id: str, job_id: str, now: datetime) -> bool:
        now = now.astimezone(UTC)
        with self._session_factory.begin() as database:
            job = database.scalar(
                select(JobRecord).where(
                    JobRecord.id == job_id,
                    JobRecord.owner_user_id == owner_user_id,
                )
            )
            if job is None:
                return False
            if job.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return True
            job.cancel_requested = True
            job.state = "CANCELLED"
            job.failure_code = "CANCELLED_BY_OWNER"
            job.completed_at = now
            imported = database.get(ImportRecord, job.import_id)
            if imported is not None:
                imported.state = "FAILED"
                imported.failure_code = job.failure_code
            return True

    def rescue_expired(self, *, now: datetime) -> int:
        now = now.astimezone(UTC)
        rescued = 0
        with self._session_factory.begin() as database:
            rows = database.scalars(
                select(JobRecord).where(
                    JobRecord.state.in_(("LEASED", "RUNNING")),
                    JobRecord.lease_expires_at <= now,
                )
            ).all()
            for job in rows:
                imported = database.get(ImportRecord, job.import_id)
                attempt = database.scalar(
                    select(JobAttemptRecord).where(
                        JobAttemptRecord.job_id == job.id,
                        JobAttemptRecord.fencing_generation == job.fencing_generation,
                    )
                )
                if attempt is not None:
                    attempt.state = "EXPIRED"
                    attempt.failure_code = "LEASE_EXPIRED"
                    attempt.completed_at = now
                if job.attempt_count >= job.max_attempts:
                    job.state = "FAILED"
                    job.failure_code = "ATTEMPT_BUDGET_EXHAUSTED"
                    job.completed_at = now
                    if imported is not None:
                        imported.state = "FAILED"
                        imported.failure_code = job.failure_code
                else:
                    job.state = "QUEUED"
                    job.not_before = now
                    if imported is not None:
                        imported.state = "QUEUED"
                job.lease_owner = None
                job.lease_expires_at = None
                rescued += 1
        return rescued

    def _conditional_state(
        self,
        lease: JobLease,
        *,
        expected_state: str,
        next_state: str,
        now: datetime,
    ) -> bool:
        now = now.astimezone(UTC)
        with self._session_factory.begin() as database:
            job = database.get(JobRecord, lease.job_id)
            if job is None:
                return False
            user = database.get(UserRecord, job.owner_user_id)
            imported = database.get(ImportRecord, job.import_id)
            if not (
                job.state == expected_state
                and _lease_matches(job, lease)
                and job.lease_expires_at is not None
                and now < _aware(job.lease_expires_at)
                and not job.cancel_requested
                and _scope_is_active(job, user, imported)
            ):
                return False
            job.state = next_state
            attempt = database.scalar(
                select(JobAttemptRecord).where(
                    JobAttemptRecord.job_id == job.id,
                    JobAttemptRecord.fencing_generation == lease.fencing_generation,
                )
            )
            if attempt is not None:
                attempt.state = next_state
            return True


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _lease_matches(job: JobRecord, lease: JobLease) -> bool:
    return job.lease_owner == lease.worker_id and job.fencing_generation == lease.fencing_generation


def _scope_is_active(
    job: JobRecord,
    user: UserRecord | None,
    imported: ImportRecord | None,
) -> bool:
    return bool(
        user is not None
        and imported is not None
        and user.lifecycle_state == "ACTIVE"
        and imported.lifecycle_state == "ACTIVE"
        and user.lifecycle_generation == job.captured_account_generation
        and imported.lifecycle_generation == job.captured_import_generation
    )


def _retry_delay(job_id: str, attempt_count: int) -> timedelta:
    bounded_attempt = min(attempt_count, 8)
    base_seconds = RETRY_BASE.total_seconds() * (2 ** (bounded_attempt - 1))
    jitter_digest = hashlib.sha256(
        b"RateReplay.RetryJitter.v1\x00" + job_id.encode() + attempt_count.to_bytes(4, "big")
    ).digest()
    jitter = int.from_bytes(jitter_digest[:2], "big") / 65_535
    return timedelta(seconds=min(300.0, base_seconds * (1 + 0.25 * jitter)))
