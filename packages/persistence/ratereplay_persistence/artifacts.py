"""Attempt-scoped artifact staging and fenced semantic-result publication."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.jobs import JobLease, current_fenced_job
from ratereplay_persistence.models import (
    JobAttemptRecord,
    JobRecord,
    JobResultClaimRecord,
    ObjectUploadRegistrationRecord,
)
from ratereplay_persistence.object_store import ObjectStore

ARTIFACT_LIMITS: Final = {"REPORT": 10 * 1024 * 1024, "TRACE": 50 * 1024 * 1024}


class ArtifactServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    registration_id: str
    object_key: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class FinalizedResult:
    claim_id: str
    result_type: str
    result_id: str
    accepted_job_id: str
    repeated: bool


class ArtifactService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_store: ObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._objects = object_store

    def stage(
        self,
        *,
        owner_user_id: str,
        lease: JobLease,
        artifact_class: str,
        source: BinaryIO,
        now: datetime,
    ) -> StagedArtifact:
        try:
            maximum_bytes = ARTIFACT_LIMITS[artifact_class]
        except KeyError as error:
            raise ArtifactServiceError(
                "UNSUPPORTED_ARTIFACT_CLASS",
                "Artifact class is not supported",
            ) from error
        now = now.astimezone(UTC)
        registration = self._register(
            owner_user_id=owner_user_id,
            lease=lease,
            artifact_class=artifact_class,
            now=now,
        )
        if registration.state == "STAGED":
            if registration.content_hash is None or registration.size_bytes is None:
                raise ArtifactServiceError(
                    "ARTIFACT_STATE_INVALID",
                    "Staged artifact metadata is incomplete",
                )
            return StagedArtifact(
                registration.id,
                registration.object_key,
                registration.content_hash,
                registration.size_bytes,
            )
        if registration.state != "REGISTERED":
            raise ArtifactServiceError(
                "ARTIFACT_ALREADY_TERMINAL",
                "Artifact registration is already terminal",
            )
        stored = self._objects.put_file(
            registration.object_key,
            source,
            maximum_bytes=maximum_bytes,
        )
        stale = False
        with self._session_factory.begin() as database:
            current = database.get(ObjectUploadRegistrationRecord, registration.id)
            job = current_fenced_job(
                database,
                lease,
                now=now,
                expected_states=frozenset({"RUNNING"}),
            )
            if (
                current is None
                or current.owner_user_id != owner_user_id
                or current.state != "REGISTERED"
                or job is None
                or job.owner_user_id != owner_user_id
            ):
                if current is not None and current.state == "REGISTERED":
                    current.state = "DELETE_PENDING"
                    current.updated_at = now
                stale = True
            else:
                current.state = "STAGED"
                current.content_hash = stored.content_hash
                current.size_bytes = stored.size_bytes
                current.updated_at = now
        if stale:
            self._delete_registration_object(registration.id, registration.object_key, now=now)
            raise ArtifactServiceError(
                "STALE_ARTIFACT_ATTEMPT",
                "Artifact attempt lost its publication fence",
            )
        return StagedArtifact(
            registration.id,
            registration.object_key,
            stored.content_hash,
            stored.size_bytes,
        )

    def finalize(
        self,
        *,
        owner_user_id: str,
        lease: JobLease,
        semantic_hash: str,
        calculation_contract_version: str,
        result_type: str,
        result_id: str,
        artifact_registration_ids: tuple[str, ...],
        now: datetime,
        publish_result: Callable[[Session], None] | None = None,
    ) -> FinalizedResult:
        if len(semantic_hash) != 64:
            raise ArtifactServiceError(
                "INVALID_SEMANTIC_HASH",
                "Semantic calculation hash must contain 64 characters",
            )
        if not calculation_contract_version or len(calculation_contract_version) > 64:
            raise ArtifactServiceError(
                "INVALID_CALCULATION_CONTRACT",
                "Calculation contract version is invalid",
            )
        if not result_type or len(result_type) > 32 or not result_id or len(result_id) > 32:
            raise ArtifactServiceError(
                "INVALID_RESULT_IDENTITY",
                "Result identity is invalid",
            )
        if len(set(artifact_registration_ids)) != len(artifact_registration_ids):
            raise ArtifactServiceError(
                "DUPLICATE_ARTIFACT_REGISTRATION",
                "Artifact registrations must be unique",
            )
        now = now.astimezone(UTC)
        try:
            return self._finalize_once(
                owner_user_id=owner_user_id,
                lease=lease,
                semantic_hash=semantic_hash,
                calculation_contract_version=calculation_contract_version,
                result_type=result_type,
                result_id=result_id,
                artifact_registration_ids=artifact_registration_ids,
                now=now,
                publish_result=publish_result,
            )
        except IntegrityError as error:
            repeated = self._resolve_semantic_race(
                owner_user_id=owner_user_id,
                lease=lease,
                semantic_hash=semantic_hash,
                now=now,
            )
            if repeated is None:
                raise ArtifactServiceError(
                    "RESULT_PUBLICATION_CONFLICT",
                    "Result publication conflicted",
                ) from error
            return repeated

    def sweep_orphans(self, *, now: datetime, older_than: datetime) -> int:
        now = now.astimezone(UTC)
        older_than = older_than.astimezone(UTC)
        pending: list[tuple[str, str]] = []
        with self._session_factory.begin() as database:
            registrations = database.scalars(
                select(ObjectUploadRegistrationRecord).where(
                    ObjectUploadRegistrationRecord.state.in_(
                        ("REGISTERED", "STAGED", "DELETE_PENDING")
                    ),
                    ObjectUploadRegistrationRecord.updated_at <= older_than,
                )
            ).all()
            for registration in registrations:
                job = database.get(JobRecord, registration.job_id)
                orphaned = bool(
                    registration.state == "DELETE_PENDING"
                    or job is None
                    or job.fencing_generation != registration.fencing_generation
                    or job.state in {"SUCCEEDED", "FAILED", "CANCELLED"}
                    or (job.lease_expires_at is not None and _aware(job.lease_expires_at) <= now)
                )
                if orphaned:
                    registration.state = "DELETE_PENDING"
                    registration.updated_at = now
                    pending.append((registration.id, registration.object_key))
        for registration_id, object_key in pending:
            self._delete_registration_object(registration_id, object_key, now=now)
        return len(pending)

    def _register(
        self,
        *,
        owner_user_id: str,
        lease: JobLease,
        artifact_class: str,
        now: datetime,
    ) -> ObjectUploadRegistrationRecord:
        with self._session_factory.begin() as database:
            existing = database.scalar(
                select(ObjectUploadRegistrationRecord).where(
                    ObjectUploadRegistrationRecord.job_id == lease.job_id,
                    ObjectUploadRegistrationRecord.fencing_generation == lease.fencing_generation,
                    ObjectUploadRegistrationRecord.artifact_class == artifact_class,
                )
            )
            if existing is not None:
                if existing.owner_user_id != owner_user_id:
                    raise ArtifactServiceError(
                        "ARTIFACT_NOT_FOUND",
                        "Artifact registration is unavailable",
                    )
                return existing
            job = current_fenced_job(
                database,
                lease,
                now=now,
                expected_states=frozenset({"RUNNING"}),
            )
            if job is None or job.owner_user_id != owner_user_id:
                raise ArtifactServiceError(
                    "STALE_ARTIFACT_ATTEMPT",
                    "Artifact attempt does not hold a live publication fence",
                )
            upload_identifier = secrets.token_hex(16)
            registration = ObjectUploadRegistrationRecord(
                id=secrets.token_hex(16),
                owner_user_id=owner_user_id,
                job_id=job.id,
                attempt_number=lease.attempt_number,
                fencing_generation=lease.fencing_generation,
                artifact_class=artifact_class,
                object_key=(
                    f"owners/{owner_user_id}/jobs/{job.id}/attempts/"
                    f"{lease.fencing_generation}/{artifact_class.lower()}-{upload_identifier}"
                ),
                upload_identifier=upload_identifier,
                state="REGISTERED",
                created_at=now,
                updated_at=now,
            )
            database.add(registration)
            database.flush()
            return registration

    def _finalize_once(
        self,
        *,
        owner_user_id: str,
        lease: JobLease,
        semantic_hash: str,
        calculation_contract_version: str,
        result_type: str,
        result_id: str,
        artifact_registration_ids: tuple[str, ...],
        now: datetime,
        publish_result: Callable[[Session], None] | None,
    ) -> FinalizedResult:
        with self._session_factory.begin() as database:
            job = database.get(JobRecord, lease.job_id)
            if job is not None and job.state == "SUCCEEDED":
                claim = database.scalar(
                    select(JobResultClaimRecord).where(
                        JobResultClaimRecord.owner_user_id == owner_user_id,
                        JobResultClaimRecord.job_kind == job.kind,
                        JobResultClaimRecord.semantic_hash == semantic_hash,
                        JobResultClaimRecord.result_type == job.terminal_result_type,
                        JobResultClaimRecord.result_id == job.terminal_result_id,
                    )
                )
                if claim is None:
                    raise ArtifactServiceError(
                        "RESULT_ALREADY_TERMINAL",
                        "Job is already terminal with another result",
                    )
                return _claim_result(claim, repeated=True)
            job = current_fenced_job(
                database,
                lease,
                now=now,
                expected_states=frozenset({"RUNNING"}),
            )
            if job is None or job.owner_user_id != owner_user_id:
                raise ArtifactServiceError(
                    "STALE_RESULT_ATTEMPT",
                    "Result attempt does not hold a live publication fence",
                )
            if job.requested_semantic_hash not in {
                None,
                semantic_hash,
            } or job.calculation_contract_version not in {None, calculation_contract_version}:
                raise ArtifactServiceError(
                    "RESULT_SEMANTIC_IDENTITY_MISMATCH",
                    "Result identity differs from the submitted calculation",
                )
            prior = database.scalar(
                select(JobResultClaimRecord).where(
                    JobResultClaimRecord.owner_user_id == owner_user_id,
                    JobResultClaimRecord.job_kind == job.kind,
                    JobResultClaimRecord.semantic_hash == semantic_hash,
                )
            )
            if prior is not None:
                _complete_job(database, job, lease, prior, now=now)
                return _claim_result(prior, repeated=True)
            registrations = tuple(
                database.scalars(
                    select(ObjectUploadRegistrationRecord).where(
                        ObjectUploadRegistrationRecord.id.in_(artifact_registration_ids)
                    )
                )
            )
            if len(registrations) != len(artifact_registration_ids) or any(
                registration.owner_user_id != owner_user_id
                or registration.job_id != job.id
                or registration.fencing_generation != lease.fencing_generation
                or registration.state != "STAGED"
                for registration in registrations
            ):
                raise ArtifactServiceError(
                    "ARTIFACT_SET_INVALID",
                    "Result artifact set is incomplete or outside the current attempt",
                )
            if publish_result is not None:
                publish_result(database)
                database.flush()
            claim = JobResultClaimRecord(
                id=secrets.token_hex(16),
                owner_user_id=owner_user_id,
                job_kind=job.kind,
                semantic_hash=semantic_hash,
                calculation_contract_version=calculation_contract_version,
                result_type=result_type,
                result_id=result_id,
                accepted_job_id=job.id,
                created_at=now,
            )
            database.add(claim)
            database.flush()
            for registration in registrations:
                registration.state = "ACCEPTED"
                registration.accepted_at = now
                registration.updated_at = now
            _complete_job(database, job, lease, claim, now=now)
            return _claim_result(claim, repeated=False)

    def _resolve_semantic_race(
        self,
        *,
        owner_user_id: str,
        lease: JobLease,
        semantic_hash: str,
        now: datetime,
    ) -> FinalizedResult | None:
        with self._session_factory.begin() as database:
            job = current_fenced_job(
                database,
                lease,
                now=now,
                expected_states=frozenset({"RUNNING"}),
            )
            claim = database.scalar(
                select(JobResultClaimRecord).where(
                    JobResultClaimRecord.owner_user_id == owner_user_id,
                    JobResultClaimRecord.job_kind == lease.kind,
                    JobResultClaimRecord.semantic_hash == semantic_hash,
                )
            )
            if job is None or job.owner_user_id != owner_user_id or claim is None:
                return None
            _complete_job(database, job, lease, claim, now=now)
            return _claim_result(claim, repeated=True)

    def _delete_registration_object(
        self,
        registration_id: str,
        object_key: str,
        *,
        now: datetime,
    ) -> None:
        self._objects.delete(object_key)
        with self._session_factory.begin() as database:
            registration = database.get(ObjectUploadRegistrationRecord, registration_id)
            if registration is not None and registration.state == "DELETE_PENDING":
                registration.state = "DELETED"
                registration.deleted_at = now
                registration.updated_at = now


def _complete_job(
    database: Session,
    job: JobRecord,
    lease: JobLease,
    claim: JobResultClaimRecord,
    *,
    now: datetime,
) -> None:
    attempt = database.scalar(
        select(JobAttemptRecord).where(
            JobAttemptRecord.job_id == job.id,
            JobAttemptRecord.fencing_generation == lease.fencing_generation,
        )
    )
    if attempt is None:
        raise ArtifactServiceError(
            "JOB_ATTEMPT_MISSING",
            "Current job attempt is unavailable",
        )
    attempt.state = "SUCCEEDED"
    attempt.completed_at = now
    job.state = "SUCCEEDED"
    job.completed_at = now
    job.terminal_result_type = claim.result_type
    job.terminal_result_id = claim.result_id
    job.terminal_semantic_hash = claim.semantic_hash


def _claim_result(claim: JobResultClaimRecord, *, repeated: bool) -> FinalizedResult:
    return FinalizedResult(
        claim_id=claim.id,
        result_type=claim.result_type,
        result_id=claim.result_id,
        accepted_job_id=claim.accepted_job_id,
        repeated=repeated,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
