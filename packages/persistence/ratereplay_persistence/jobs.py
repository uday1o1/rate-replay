"""PostgreSQL-leased job execution with attempt fencing and bounded retry."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.audit import (
    AuditEventType,
    AuditOutcome,
    append_audit_event,
)
from ratereplay_persistence.models import (
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    ProfileVersionRecord,
    UserRecord,
)

LEASE_DURATION = timedelta(seconds=20)
RETRY_BASE = timedelta(seconds=2)


@dataclass(frozen=True, slots=True)
class JobDefinition:
    kind: str
    scope_mode: str
    requires_import: bool
    requires_profile: bool


JOB_DEFINITIONS: Final = MappingProxyType(
    {
        "IMPORT": JobDefinition("IMPORT", "ACTIVE_SCOPE", True, False),
        "REPLAY": JobDefinition("REPLAY", "ACTIVE_SCOPE", True, True),
        "COMPARISON": JobDefinition("COMPARISON", "ACTIVE_SCOPE", True, True),
        "SCENARIO": JobDefinition("SCENARIO", "ACTIVE_SCOPE", True, True),
        "REPORT": JobDefinition("REPORT", "ACTIVE_SCOPE", True, True),
        "RETENTION": JobDefinition("RETENTION", "SYSTEM_SCOPE", False, False),
        "DELETION": JobDefinition("DELETION", "DELETING_SCOPE", False, False),
    }
)


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    import_id: str | None
    worker_id: str
    attempt_number: int
    fencing_generation: int
    lease_expires_at: datetime
    kind: str = "IMPORT"
    profile_version_id: str | None = None
    scope_mode: str = "ACTIVE_SCOPE"


class JobService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def lease_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        kinds: frozenset[str] | None = None,
    ) -> JobLease | None:
        now = now.astimezone(UTC)
        self.rescue_expired(now=now)
        selected_kinds = frozenset(JOB_DEFINITIONS) if kinds is None else kinds
        unknown_kinds = selected_kinds.difference(JOB_DEFINITIONS)
        if unknown_kinds:
            raise ValueError(f"Unknown job kinds: {', '.join(sorted(unknown_kinds))}")
        if not selected_kinds:
            return None
        with self._session_factory.begin() as database:
            while True:
                query = (
                    select(JobRecord)
                    .where(
                        JobRecord.state == "QUEUED",
                        JobRecord.not_before <= now,
                        JobRecord.cancel_requested.is_(False),
                        JobRecord.kind.in_(selected_kinds),
                    )
                    .order_by(JobRecord.created_at, JobRecord.id)
                    .limit(1)
                )
                if database.bind is not None and database.bind.dialect.name == "postgresql":
                    query = query.with_for_update(skip_locked=True)
                job = database.scalar(query)
                if job is None:
                    return None
                definition = JOB_DEFINITIONS.get(job.kind)
                if definition is None:
                    _finish_without_lease(
                        job,
                        state="FAILED",
                        code="UNKNOWN_JOB_KIND",
                        now=now,
                    )
                    database.flush()
                    continue
                user, imported, profile = _load_scope(database, job)
                if not _scope_is_valid(job, definition, user, imported, profile):
                    _finish_without_lease(job, state="CANCELLED", code="SCOPE_FENCED", now=now)
                    database.flush()
                    continue
                if job.attempt_count >= job.max_attempts:
                    _finish_without_lease(
                        job,
                        state="FAILED",
                        code="ATTEMPT_BUDGET_EXHAUSTED",
                        now=now,
                    )
                    _mark_import_terminal(job, imported, job.failure_code)
                    database.flush()
                    continue
                job.attempt_count += 1
                job.fencing_generation += 1
                job.state = "LEASED"
                job.lease_owner = worker_id
                job.lease_acquired_at = now
                job.heartbeat_at = now
                job.lease_expires_at = now + LEASE_DURATION
                if job.kind == "IMPORT" and imported is not None:
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
                append_audit_event(
                    database,
                    owner_user_id=job.owner_user_id,
                    event_type="JOB_LEASED",
                    subject_type="JOB",
                    subject_id=job.id,
                    sequence=job.fencing_generation,
                    outcome="LEASED",
                    now=now,
                )
                return JobLease(
                    job_id=job.id,
                    import_id=job.import_id,
                    worker_id=worker_id,
                    attempt_number=job.attempt_count,
                    fencing_generation=job.fencing_generation,
                    lease_expires_at=job.lease_expires_at,
                    kind=job.kind,
                    profile_version_id=job.profile_version_id,
                    scope_mode=job.scope_mode,
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
            definition = JOB_DEFINITIONS.get(job.kind)
            user, imported, profile = _load_scope(database, job)
            if not (
                job.state in {"LEASED", "RUNNING"}
                and _lease_matches(job, lease)
                and job.lease_expires_at is not None
                and now < _aware(job.lease_expires_at)
                and definition is not None
                and _scope_is_valid(job, definition, user, imported, profile)
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
            definition = JOB_DEFINITIONS.get(job.kind)
            user, imported, profile = _load_scope(database, job)
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
            if definition is None or not _scope_is_valid(job, definition, user, imported, profile):
                job.state = "CANCELLED"
                job.failure_code = "SCOPE_FENCED"
                job.completed_at = now
                if attempt is not None:
                    attempt.state = "CANCELLED"
                    attempt.failure_code = job.failure_code
                job.lease_owner = None
                job.lease_expires_at = None
                append_audit_event(
                    database,
                    owner_user_id=job.owner_user_id,
                    event_type="JOB_CANCELLED",
                    subject_type="JOB",
                    subject_id=job.id,
                    sequence=job.fencing_generation,
                    outcome="CANCELLED",
                    now=now,
                )
                return True
            if retryable and job.attempt_count < job.max_attempts:
                job.state = "QUEUED"
                job.not_before = now + _retry_delay(job.id, job.attempt_count)
                if job.kind == "IMPORT" and imported is not None:
                    imported.state = "QUEUED"
                append_audit_event(
                    database,
                    owner_user_id=job.owner_user_id,
                    event_type="JOB_RETRY_SCHEDULED",
                    subject_type="JOB",
                    subject_id=job.id,
                    sequence=job.fencing_generation,
                    outcome="RETRY_SCHEDULED",
                    now=now,
                )
            else:
                job.state = "FAILED"
                job.failure_code = code if not retryable else "ATTEMPT_BUDGET_EXHAUSTED"
                job.completed_at = now
                _mark_import_terminal(job, imported, job.failure_code)
                append_audit_event(
                    database,
                    owner_user_id=job.owner_user_id,
                    event_type="JOB_FAILED",
                    subject_type="JOB",
                    subject_id=job.id,
                    sequence=job.fencing_generation,
                    outcome="FAILED",
                    now=now,
                )
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
            imported = database.get(ImportRecord, job.import_id) if job.import_id else None
            if job.kind == "IMPORT" and imported is not None:
                imported.state = "FAILED"
                imported.failure_code = job.failure_code
            append_audit_event(
                database,
                owner_user_id=job.owner_user_id,
                event_type="JOB_CANCELLED",
                subject_type="JOB",
                subject_id=job.id,
                sequence=job.fencing_generation,
                outcome="CANCELLED",
                now=now,
            )
            return True

    def complete_system(self, lease: JobLease, *, now: datetime) -> bool:
        """Complete a fenced system-scope retention job without publishing user data."""

        if lease.kind != "RETENTION" or lease.scope_mode != "SYSTEM_SCOPE":
            raise ValueError("Only a SYSTEM_SCOPE retention lease can use system completion")
        now = now.astimezone(UTC)
        with self._session_factory.begin() as database:
            job = current_fenced_job(
                database,
                lease,
                now=now,
                expected_states=frozenset({"RUNNING"}),
            )
            if job is None:
                return False
            attempt = database.scalar(
                select(JobAttemptRecord).where(
                    JobAttemptRecord.job_id == job.id,
                    JobAttemptRecord.fencing_generation == lease.fencing_generation,
                )
            )
            if attempt is None:
                return False
            attempt.state = "SUCCEEDED"
            attempt.completed_at = now
            job.state = "SUCCEEDED"
            job.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            append_audit_event(
                database,
                owner_user_id=job.owner_user_id,
                event_type="JOB_SUCCEEDED",
                subject_type="JOB",
                subject_id=job.id,
                sequence=job.fencing_generation,
                outcome="SUCCEEDED",
                now=now,
            )
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
                audit_event_type: AuditEventType
                audit_outcome: AuditOutcome
                definition = JOB_DEFINITIONS.get(job.kind)
                user, imported, profile = _load_scope(database, job)
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
                if definition is None or not _scope_is_valid(
                    job, definition, user, imported, profile
                ):
                    job.state = "CANCELLED"
                    job.failure_code = "SCOPE_FENCED"
                    job.completed_at = now
                    audit_event_type = "JOB_CANCELLED"
                    audit_outcome = "CANCELLED"
                elif job.attempt_count >= job.max_attempts:
                    job.state = "FAILED"
                    job.failure_code = "ATTEMPT_BUDGET_EXHAUSTED"
                    job.completed_at = now
                    _mark_import_terminal(job, imported, job.failure_code)
                    audit_event_type = "JOB_FAILED"
                    audit_outcome = "FAILED"
                else:
                    job.state = "QUEUED"
                    job.not_before = now
                    if job.kind == "IMPORT" and imported is not None:
                        imported.state = "QUEUED"
                    audit_event_type = "JOB_RETRY_SCHEDULED"
                    audit_outcome = "RETRY_SCHEDULED"
                append_audit_event(
                    database,
                    owner_user_id=job.owner_user_id,
                    event_type=audit_event_type,
                    subject_type="JOB",
                    subject_id=job.id,
                    sequence=job.fencing_generation,
                    outcome=audit_outcome,
                    now=now,
                )
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
            definition = JOB_DEFINITIONS.get(job.kind)
            user, imported, profile = _load_scope(database, job)
            if not (
                job.state == expected_state
                and _lease_matches(job, lease)
                and job.lease_expires_at is not None
                and now < _aware(job.lease_expires_at)
                and not job.cancel_requested
                and definition is not None
                and _scope_is_valid(job, definition, user, imported, profile)
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
    return bool(
        job.lease_owner == lease.worker_id
        and job.fencing_generation == lease.fencing_generation
        and job.kind == lease.kind
        and job.scope_mode == lease.scope_mode
        and job.import_id == lease.import_id
        and job.profile_version_id == lease.profile_version_id
    )


def current_fenced_job(
    database: Session,
    lease: JobLease,
    *,
    now: datetime,
    expected_states: frozenset[str],
) -> JobRecord | None:
    """Return the exact live fenced job only while every captured scope remains valid."""

    job = database.get(JobRecord, lease.job_id)
    if job is None:
        return None
    definition = JOB_DEFINITIONS.get(job.kind)
    user, imported, profile = _load_scope(database, job)
    if not (
        job.state in expected_states
        and _lease_matches(job, lease)
        and job.lease_expires_at is not None
        and now.astimezone(UTC) < _aware(job.lease_expires_at)
        and not job.cancel_requested
        and definition is not None
        and _scope_is_valid(job, definition, user, imported, profile)
    ):
        return None
    return job


def _load_scope(
    database: Session,
    job: JobRecord,
) -> tuple[UserRecord | None, ImportRecord | None, ProfileVersionRecord | None]:
    user = database.get(UserRecord, job.owner_user_id) if job.owner_user_id else None
    imported = database.get(ImportRecord, job.import_id) if job.import_id else None
    profile = (
        database.get(ProfileVersionRecord, job.profile_version_id)
        if job.profile_version_id
        else None
    )
    return user, imported, profile


def _scope_is_valid(
    job: JobRecord,
    definition: JobDefinition,
    user: UserRecord | None,
    imported: ImportRecord | None,
    profile: ProfileVersionRecord | None,
) -> bool:
    if job.scope_mode != definition.scope_mode:
        return False
    if definition.scope_mode == "SYSTEM_SCOPE":
        return bool(
            job.owner_user_id is None
            and job.import_id is None
            and job.profile_version_id is None
            and job.captured_account_generation == 0
            and job.captured_import_generation is None
            and job.captured_profile_generation is None
        )
    if user is None or user.lifecycle_generation != job.captured_account_generation:
        return False
    expected_lifecycle = "ACTIVE" if definition.scope_mode == "ACTIVE_SCOPE" else "DELETING"
    if user.lifecycle_state != expected_lifecycle:
        return False
    if definition.requires_import:
        if (
            imported is None
            or imported.owner_user_id != job.owner_user_id
            or imported.lifecycle_state != "ACTIVE"
            or imported.lifecycle_generation != job.captured_import_generation
        ):
            return False
    elif job.import_id is not None or job.captured_import_generation is not None:
        return False
    if definition.requires_profile:
        return bool(
            profile is not None
            and profile.owner_user_id == job.owner_user_id
            and profile.import_id == job.import_id
            and profile.lifecycle_state == "ACTIVE"
            and profile.lifecycle_generation == job.captured_profile_generation
        )
    return job.profile_version_id is None and job.captured_profile_generation is None


def _finish_without_lease(
    job: JobRecord,
    *,
    state: str,
    code: str,
    now: datetime,
) -> None:
    job.state = state
    job.failure_code = code
    job.completed_at = now
    job.lease_owner = None
    job.lease_expires_at = None


def _mark_import_terminal(
    job: JobRecord,
    imported: ImportRecord | None,
    code: str | None,
) -> None:
    if job.kind == "IMPORT" and imported is not None:
        imported.state = "FAILED"
        imported.failure_code = code


def _retry_delay(job_id: str, attempt_count: int) -> timedelta:
    bounded_attempt = min(attempt_count, 8)
    base_seconds = RETRY_BASE.total_seconds() * (2 ** (bounded_attempt - 1))
    jitter_digest = hashlib.sha256(
        b"RateReplay.RetryJitter.v1\x00" + job_id.encode() + attempt_count.to_bytes(4, "big")
    ).digest()
    jitter = int.from_bytes(jitter_digest[:2], "big") / 65_535
    return timedelta(seconds=min(300.0, base_seconds * (1 + 0.25 * jitter)))
