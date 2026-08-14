"""Fenced, resumable account-deletion drain, sweep, verify, and completion."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import ColumnElement

from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger, LedgerEvent
from ratereplay_persistence.deletions import (
    RECEIPT_LIFETIME,
    DeletionCoordinator,
    DeletionServiceError,
    _event_arguments,
    _validate_control_event,
)
from ratereplay_persistence.jobs import JobLease, current_fenced_job
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ComparisonResultRecord,
    DeletionAuditRecord,
    DeletionControlOperationRecord,
    DeletionFenceTargetRecord,
    DeletionIntentRecord,
    DeletionLedgerReceiptRecord,
    DeletionReceiptRecord,
    ImportFindingRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    JobResultClaimRecord,
    ObjectUploadRegistrationRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    RawObjectRecord,
    ReplayResultRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
    ScenarioResultRecord,
    SessionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore


class DeletionSweepError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    state: Literal["ADVANCED", "PENDING", "COMPLETED"]
    phase: str


class DeletionSweepService:
    """Advance exactly one persisted deletion phase under a live job fence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_store: FilesystemObjectStore,
        ledger: FilesystemDeletionLedger,
    ) -> None:
        self._session_factory = session_factory
        self._objects = object_store
        self._ledger = ledger

    def advance(self, lease: JobLease, *, now: datetime) -> SweepOutcome:
        now = _aware(now)
        if lease.kind != "DELETION" or lease.scope_mode != "DELETING_SCOPE":
            raise DeletionSweepError(
                "INVALID_DELETION_LEASE",
                "Deletion sweep requires a DELETING_SCOPE lease",
            )
        control = self._control_for_lease(lease, now=now)
        if control.phase == "DRAIN":
            return self._drain(lease, control, now=now)
        if control.phase == "SWEEP":
            return self._sweep(lease, control, now=now)
        if control.phase == "VERIFY":
            return self._verify(lease, control, now=now)
        if control.phase == "COMPLETE":
            return self._complete(lease, control, now=now)
        raise DeletionSweepError(
            "INVALID_DELETION_PHASE",
            "Deletion control phase cannot be advanced by the sweep worker",
        )

    def expire_receipt_verifiers(self, *, now: datetime) -> int:
        """Destroy completed receipt verifiers at their exact fixed expiry."""

        now = _aware(now)
        expired = 0
        with self._session_factory.begin() as database:
            receipts = database.scalars(
                select(DeletionReceiptRecord).where(
                    DeletionReceiptRecord.status == "DELETED",
                    DeletionReceiptRecord.verifier_expires_at <= now,
                )
            ).all()
            for receipt in receipts:
                audit = database.get(DeletionAuditRecord, receipt.deletion_id)
                if audit is None:
                    raise DeletionSweepError(
                        "DELETION_AUDIT_MISSING",
                        "Completed deletion is missing its minimum audit tombstone",
                    )
                audit.receipt_verifier = None
                database.delete(receipt)
                expired += 1
        return expired

    def _control_for_lease(
        self,
        lease: JobLease,
        *,
        now: datetime,
    ) -> DeletionControlOperationRecord:
        with self._session_factory() as database:
            job = current_fenced_job(
                database,
                lease,
                now=now,
                expected_states=frozenset({"RUNNING"}),
            )
            control = database.scalar(
                select(DeletionControlOperationRecord).where(
                    DeletionControlOperationRecord.deletion_job_id == lease.job_id
                )
            )
            if job is None or control is None or job.owner_user_id is None:
                raise DeletionSweepError(
                    "STALE_DELETION_LEASE",
                    "Deletion sweep lost its exact job or lifecycle fence",
                )
            database.expunge(control)
        self._requested_event(control)
        return control

    def _requested_event(self, control: DeletionControlOperationRecord) -> LedgerEvent:
        chain = self._ledger.chain(control.deletion_id)
        requested = next((event for event in chain if event.phase == "REQUESTED"), None)
        if requested is None:
            raise DeletionSweepError(
                "REQUESTED_LEDGER_MISSING",
                "Deletion cannot sweep without a durable REQUESTED event",
            )
        try:
            _validate_control_event(requested, control, expected_phase="REQUESTED")
        except DeletionServiceError as error:
            raise DeletionSweepError(error.code, str(error)) from error
        return requested

    def _drain(
        self,
        lease: JobLease,
        snapshot: DeletionControlOperationRecord,
        *,
        now: datetime,
    ) -> SweepOutcome:
        self._requested_event(snapshot)
        cleanup: list[tuple[str, str]] = []
        pending = False
        with self._session_factory.begin() as database:
            control, user = _locked_control(database, lease, phase="DRAIN", now=now)
            ordinary_jobs = database.scalars(
                select(JobRecord).where(
                    JobRecord.owner_user_id == user.id,
                    JobRecord.id != lease.job_id,
                    JobRecord.captured_account_generation < control.deletion_generation,
                )
            ).all()
            ordinary_ids = tuple(job.id for job in ordinary_jobs)
            jobs_by_id = {job.id: job for job in ordinary_jobs}
            attempts = (
                database.scalars(
                    select(JobAttemptRecord).where(JobAttemptRecord.job_id.in_(ordinary_ids))
                ).all()
                if ordinary_ids
                else []
            )
            for attempt in attempts:
                fence = _fence_target(
                    database,
                    deletion_id=control.deletion_id,
                    target_kind="JOB_ATTEMPT",
                    target_id=attempt.id,
                    generation=attempt.fencing_generation,
                    state=attempt.state,
                    now=now,
                )
                job = jobs_by_id[attempt.job_id]
                live = bool(
                    job.state in {"LEASED", "RUNNING"}
                    and job.lease_expires_at is not None
                    and now < _aware(job.lease_expires_at)
                    and attempt.fencing_generation == job.fencing_generation
                )
                if live:
                    pending = True
                elif fence.resolved_at is None:
                    fence.resolved_at = now
            uploads = database.scalars(
                select(ObjectUploadRegistrationRecord).where(
                    ObjectUploadRegistrationRecord.owner_user_id == user.id
                )
            ).all()
            for upload in uploads:
                fence = _fence_target(
                    database,
                    deletion_id=control.deletion_id,
                    target_kind="UPLOAD",
                    target_id=upload.id,
                    generation=upload.fencing_generation,
                    state=upload.state,
                    now=now,
                )
                parent = database.get(JobRecord, upload.job_id)
                parent_live = bool(
                    parent is not None
                    and parent.state in {"LEASED", "RUNNING"}
                    and parent.lease_expires_at is not None
                    and now < _aware(parent.lease_expires_at)
                    and parent.fencing_generation == upload.fencing_generation
                )
                if upload.state == "REGISTERED" and parent_live:
                    pending = True
                elif upload.state in {"REGISTERED", "STAGED", "DELETE_PENDING"}:
                    upload.state = "DELETE_PENDING"
                    upload.updated_at = now
                    cleanup.append((upload.id, upload.object_key))
                elif upload.state == "DELETED" and fence.resolved_at is None:
                    fence.resolved_at = now
        for registration_id, key in cleanup:
            self._objects.delete(key)
            with self._session_factory.begin() as database:
                registration = database.get(ObjectUploadRegistrationRecord, registration_id)
                if registration is not None and registration.state == "DELETE_PENDING":
                    registration.state = "DELETED"
                    registration.updated_at = now
                    registration.deleted_at = now
                upload_fence = database.scalar(
                    select(DeletionFenceTargetRecord).where(
                        DeletionFenceTargetRecord.deletion_id == snapshot.deletion_id,
                        DeletionFenceTargetRecord.target_kind == "UPLOAD",
                        DeletionFenceTargetRecord.target_id == registration_id,
                    )
                )
                if upload_fence is not None:
                    upload_fence.resolved_at = now
        with self._session_factory.begin() as database:
            control, user = _locked_control(database, lease, phase="DRAIN", now=now)
            live_jobs = database.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(
                    JobRecord.owner_user_id == user.id,
                    JobRecord.id != lease.job_id,
                    JobRecord.captured_account_generation < control.deletion_generation,
                    JobRecord.state.in_(("LEASED", "RUNNING")),
                    JobRecord.lease_expires_at > now,
                )
            )
            live_uploads = database.scalar(
                select(func.count())
                .select_from(ObjectUploadRegistrationRecord)
                .where(
                    ObjectUploadRegistrationRecord.owner_user_id == user.id,
                    ObjectUploadRegistrationRecord.state == "REGISTERED",
                )
            )
            if pending or (live_jobs or 0) > 0 or (live_uploads or 0) > 0:
                return SweepOutcome("PENDING", "DRAIN")
            control.phase = "SWEEP"
            control.updated_at = now
            receipt = _receipt(database, control.deletion_id)
            receipt.status = "SWEEP"
            return SweepOutcome("ADVANCED", "SWEEP")

    def _sweep(
        self,
        lease: JobLease,
        snapshot: DeletionControlOperationRecord,
        *,
        now: datetime,
    ) -> SweepOutcome:
        self._requested_event(snapshot)
        with self._session_factory() as database:
            _control, user = _read_control(database, lease, phase="SWEEP", now=now)
            owner_id = user.id
        object_keys = self._objects.list_prefix(f"owners/{owner_id}")
        for key in object_keys:
            self._objects.delete(key)
        with self._session_factory.begin() as database:
            control, user = _locked_control(database, lease, phase="SWEEP", now=now)
            counts = _sweep_owner_rows(
                database,
                owner_user_id=user.id,
                deletion_job_id=lease.job_id,
            )
            counts["objects"] = len(object_keys)
            encoded_counts = json.dumps(counts, sort_keys=True, separators=(",", ":"))
            control.artifact_counts_json = encoded_counts
            control.phase = "VERIFY"
            control.updated_at = now
            receipt = _receipt(database, control.deletion_id)
            receipt.status = "VERIFY"
            receipt.artifact_counts_json = encoded_counts
        return SweepOutcome("ADVANCED", "VERIFY")

    def _verify(
        self,
        lease: JobLease,
        snapshot: DeletionControlOperationRecord,
        *,
        now: datetime,
    ) -> SweepOutcome:
        self._requested_event(snapshot)
        with self._session_factory() as database:
            _control, user = _read_control(database, lease, phase="VERIFY", now=now)
            owner_id = user.id
            prohibited = _prohibited_row_count(
                database,
                owner_user_id=owner_id,
                deletion_job_id=lease.job_id,
            )
        objects = self._objects.list_prefix(f"owners/{owner_id}")
        with self._session_factory.begin() as database:
            control, _user = _locked_control(database, lease, phase="VERIFY", now=now)
            receipt = _receipt(database, control.deletion_id)
            if objects or prohibited:
                control.phase = "SWEEP"
                control.updated_at = now
                receipt.status = "SWEEP"
                return SweepOutcome("ADVANCED", "SWEEP")
            control.phase = "COMPLETE"
            control.updated_at = now
            receipt.status = "COMPLETE"
        return SweepOutcome("ADVANCED", "COMPLETE")

    def _complete(
        self,
        lease: JobLease,
        snapshot: DeletionControlOperationRecord,
        *,
        now: datetime,
    ) -> SweepOutcome:
        requested = self._requested_event(snapshot)
        chain = self._ledger.chain(snapshot.deletion_id)
        completed = next((event for event in chain if event.phase == "COMPLETED"), None)
        if completed is None:
            completed = self._ledger.append(
                **_event_arguments(requested, phase="COMPLETED", occurred_at=now)
            )
        with self._session_factory.begin() as database:
            control, user = _locked_control(database, lease, phase="COMPLETE", now=now)
            try:
                _validate_control_event(completed, control, expected_phase="COMPLETED")
            except DeletionServiceError as error:
                raise DeletionSweepError(error.code, str(error)) from error
            DeletionCoordinator._store_ledger_receipt(database, completed)
            receipt = _receipt(database, control.deletion_id)
            if receipt.receipt_verifier is None:
                raise DeletionSweepError(
                    "DELETION_RECEIPT_MISSING",
                    "Deletion cannot finalize without its receipt verifier",
                )
            verifier_expiry = now + RECEIPT_LIFETIME
            database.add(
                DeletionAuditRecord(
                    deletion_id=control.deletion_id,
                    receipt_verifier=receipt.receipt_verifier,
                    verifier_expires_at=verifier_expiry,
                    scope_token=control.scope_token,
                    restore_key_version=control.restore_key_version,
                    deletion_generation=control.deletion_generation,
                    completed_at=now,
                    artifact_counts_json=control.artifact_counts_json,
                    status="DELETED",
                    status_code="VERIFIED_COMPLETE",
                )
            )
            receipt.status = "DELETED"
            receipt.completed_at = now
            receipt.verifier_expires_at = verifier_expiry
            receipt.artifact_counts_json = control.artifact_counts_json
            job = database.get(JobRecord, lease.job_id)
            attempt = database.scalar(
                select(JobAttemptRecord).where(
                    JobAttemptRecord.job_id == lease.job_id,
                    JobAttemptRecord.fencing_generation == lease.fencing_generation,
                )
            )
            if job is None or attempt is None:
                raise DeletionSweepError(
                    "DELETION_JOB_MISSING",
                    "Deletion terminal transaction lost its fenced job attempt",
                )
            job.state = "SUCCEEDED"
            job.completed_at = now
            attempt.state = "SUCCEEDED"
            attempt.completed_at = now
            user.lifecycle_state = "DELETED"
            database.flush()
            options = {"synchronize_session": False}
            database.execute(
                delete(DeletionFenceTargetRecord).where(
                    DeletionFenceTargetRecord.deletion_id == control.deletion_id
                ),
                execution_options=options,
            )
            database.execute(
                delete(DeletionLedgerReceiptRecord).where(
                    DeletionLedgerReceiptRecord.deletion_id == control.deletion_id
                ),
                execution_options=options,
            )
            database.execute(
                delete(DeletionIntentRecord).where(
                    DeletionIntentRecord.deletion_id == control.deletion_id
                ),
                execution_options=options,
            )
            database.execute(
                delete(DeletionControlOperationRecord).where(
                    DeletionControlOperationRecord.deletion_id == control.deletion_id
                ),
                execution_options=options,
            )
            database.execute(
                delete(JobAttemptRecord).where(JobAttemptRecord.job_id == lease.job_id),
                execution_options=options,
            )
            database.execute(
                delete(JobRecord).where(JobRecord.id == lease.job_id),
                execution_options=options,
            )
            database.execute(
                delete(UserRecord).where(UserRecord.id == user.id),
                execution_options=options,
            )
        return SweepOutcome("COMPLETED", "DELETED")


def _read_control(
    database: Session,
    lease: JobLease,
    *,
    phase: str,
    now: datetime,
) -> tuple[DeletionControlOperationRecord, UserRecord]:
    job = current_fenced_job(
        database,
        lease,
        now=now,
        expected_states=frozenset({"RUNNING"}),
    )
    control = database.scalar(
        select(DeletionControlOperationRecord).where(
            DeletionControlOperationRecord.deletion_job_id == lease.job_id
        )
    )
    user = database.get(UserRecord, job.owner_user_id) if job and job.owner_user_id else None
    if (
        job is None
        or control is None
        or user is None
        or control.phase != phase
        or user.deletion_scope_id != control.target_scope_id
    ):
        raise DeletionSweepError(
            "STALE_DELETION_LEASE",
            "Deletion phase lost its exact control and lifecycle fence",
        )
    return control, user


def _locked_control(
    database: Session,
    lease: JobLease,
    *,
    phase: str,
    now: datetime,
) -> tuple[DeletionControlOperationRecord, UserRecord]:
    control = database.scalar(
        select(DeletionControlOperationRecord)
        .where(DeletionControlOperationRecord.deletion_job_id == lease.job_id)
        .with_for_update()
    )
    if control is None:
        raise DeletionSweepError("STALE_DELETION_LEASE", "Deletion control is missing")
    user = database.scalar(
        select(UserRecord)
        .where(UserRecord.deletion_scope_id == control.target_scope_id)
        .with_for_update()
    )
    job = current_fenced_job(
        database,
        lease,
        now=now,
        expected_states=frozenset({"RUNNING"}),
    )
    if (
        user is None
        or job is None
        or control.phase != phase
        or user.lifecycle_state != "DELETING"
        or user.lifecycle_generation != control.deletion_generation
    ):
        raise DeletionSweepError(
            "STALE_DELETION_LEASE",
            "Deletion phase lost its exact control and lifecycle fence",
        )
    return control, user


def _receipt(database: Session, deletion_id: str) -> DeletionReceiptRecord:
    receipt = database.get(DeletionReceiptRecord, deletion_id)
    if receipt is None:
        raise DeletionSweepError(
            "DELETION_RECEIPT_MISSING",
            "Deletion receipt control row is missing",
        )
    return receipt


def _fence_target(
    database: Session,
    *,
    deletion_id: str,
    target_kind: Literal["JOB_ATTEMPT", "UPLOAD"],
    target_id: str,
    generation: int,
    state: str,
    now: datetime,
) -> DeletionFenceTargetRecord:
    existing = database.scalar(
        select(DeletionFenceTargetRecord).where(
            DeletionFenceTargetRecord.deletion_id == deletion_id,
            DeletionFenceTargetRecord.target_kind == target_kind,
            DeletionFenceTargetRecord.target_id == target_id,
        )
    )
    if existing is not None:
        if existing.observed_generation != generation:
            raise DeletionSweepError(
                "DELETION_FENCE_MISMATCH",
                "Deletion writer fence changed generation",
            )
        return existing
    created = DeletionFenceTargetRecord(
        id=secrets.token_hex(16),
        deletion_id=deletion_id,
        target_kind=target_kind,
        target_id=target_id,
        observed_generation=generation,
        observed_state=state,
        created_at=now,
    )
    database.add(created)
    return created


def _sweep_owner_rows(
    database: Session,
    *,
    owner_user_id: str,
    deletion_job_id: str,
) -> dict[str, int]:
    import_ids = tuple(
        database.scalars(select(ImportRecord.id).where(ImportRecord.owner_user_id == owner_user_id))
    )
    profile_ids = tuple(
        database.scalars(
            select(ProfileVersionRecord.id).where(
                ProfileVersionRecord.owner_user_id == owner_user_id
            )
        )
    )
    ordinary_job_ids = tuple(
        database.scalars(
            select(JobRecord.id).where(
                JobRecord.owner_user_id == owner_user_id,
                JobRecord.id != deletion_job_id,
            )
        )
    )
    replay_ids = tuple(
        database.scalars(
            select(ReplayResultRecord.id).where(ReplayResultRecord.owner_user_id == owner_user_id)
        )
    )
    scenario_ids = tuple(
        database.scalars(
            select(ScenarioRecord.id).where(ScenarioRecord.owner_user_id == owner_user_id)
        )
    )
    scenario_result_ids = tuple(
        database.scalars(
            select(ScenarioResultRecord.id).where(
                ScenarioResultRecord.owner_user_id == owner_user_id
            )
        )
    )
    scenario_load_ids = (
        tuple(
            database.scalars(
                select(ScenarioLoadRecord.id).where(
                    ScenarioLoadRecord.scenario_id.in_(scenario_ids)
                )
            )
        )
        if scenario_ids
        else ()
    )
    counts = {
        "calculation_manifests": _count(
            database,
            CalculationManifestRecord,
            (
                CalculationManifestRecord.replay_id.in_(replay_ids)
                | CalculationManifestRecord.scenario_result_id.in_(scenario_result_ids)
            ),
        ),
        "comparisons": _count(
            database,
            ComparisonResultRecord,
            ComparisonResultRecord.owner_user_id == owner_user_id,
        ),
        "imports": len(import_ids),
        "interval_readings": (
            _count(database, ImportReadingRecord, ImportReadingRecord.import_id.in_(import_ids))
            if import_ids
            else 0
        ),
        "jobs": len(ordinary_job_ids),
        "object_uploads": _count(
            database,
            ObjectUploadRegistrationRecord,
            ObjectUploadRegistrationRecord.owner_user_id == owner_user_id,
        ),
        "operation_requests": _count(
            database,
            OperationRequestRecord,
            OperationRequestRecord.owner_user_id == owner_user_id,
        ),
        "profiles": len(profile_ids),
        "quality_findings": (
            _count(database, ImportFindingRecord, ImportFindingRecord.import_id.in_(import_ids))
            if import_ids
            else 0
        ),
        "raw_objects": _count(
            database,
            RawObjectRecord,
            RawObjectRecord.owner_user_id == owner_user_id,
        ),
        "replays": len(replay_ids),
        "scenario_loads": len(scenario_load_ids),
        "scenario_results": len(scenario_result_ids),
        "scenarios": len(scenario_ids),
        "sessions": _count(database, SessionRecord, SessionRecord.user_id == owner_user_id),
    }
    if replay_ids or scenario_result_ids:
        database.execute(
            delete(CalculationManifestRecord).where(
                CalculationManifestRecord.replay_id.in_(replay_ids)
                | CalculationManifestRecord.scenario_result_id.in_(scenario_result_ids)
            )
        )
    if scenario_load_ids:
        database.execute(
            delete(ScenarioReferenceScheduleRecord).where(
                ScenarioReferenceScheduleRecord.scenario_load_id.in_(scenario_load_ids)
            )
        )
    database.execute(
        delete(ComparisonResultRecord).where(ComparisonResultRecord.owner_user_id == owner_user_id)
    )
    database.execute(
        delete(ScenarioResultRecord).where(ScenarioResultRecord.owner_user_id == owner_user_id)
    )
    if scenario_ids:
        database.execute(
            delete(ScenarioLoadRecord).where(ScenarioLoadRecord.scenario_id.in_(scenario_ids))
        )
    database.execute(delete(ScenarioRecord).where(ScenarioRecord.owner_user_id == owner_user_id))
    database.execute(
        delete(ReplayResultRecord).where(ReplayResultRecord.owner_user_id == owner_user_id)
    )
    database.execute(
        delete(JobResultClaimRecord).where(JobResultClaimRecord.owner_user_id == owner_user_id)
    )
    database.execute(
        delete(ObjectUploadRegistrationRecord).where(
            ObjectUploadRegistrationRecord.owner_user_id == owner_user_id
        )
    )
    if ordinary_job_ids:
        database.execute(
            delete(JobAttemptRecord).where(JobAttemptRecord.job_id.in_(ordinary_job_ids))
        )
        database.execute(delete(JobRecord).where(JobRecord.id.in_(ordinary_job_ids)))
    database.execute(
        delete(OperationRequestRecord).where(OperationRequestRecord.owner_user_id == owner_user_id)
    )
    if import_ids:
        database.execute(
            delete(ImportFindingRecord).where(ImportFindingRecord.import_id.in_(import_ids))
        )
        database.execute(
            delete(ImportReadingRecord).where(ImportReadingRecord.import_id.in_(import_ids))
        )
    database.execute(delete(RawObjectRecord).where(RawObjectRecord.owner_user_id == owner_user_id))
    database.execute(
        delete(ProfileVersionRecord).where(ProfileVersionRecord.owner_user_id == owner_user_id)
    )
    database.execute(delete(ImportRecord).where(ImportRecord.owner_user_id == owner_user_id))
    database.execute(delete(SessionRecord).where(SessionRecord.user_id == owner_user_id))
    database.execute(
        delete(DeletionIntentRecord).where(DeletionIntentRecord.owner_user_id == owner_user_id)
    )
    return counts


def _prohibited_row_count(
    database: Session,
    *,
    owner_user_id: str,
    deletion_job_id: str,
) -> int:
    direct = (
        (SessionRecord, SessionRecord.user_id == owner_user_id),
        (OperationRequestRecord, OperationRequestRecord.owner_user_id == owner_user_id),
        (ImportRecord, ImportRecord.owner_user_id == owner_user_id),
        (RawObjectRecord, RawObjectRecord.owner_user_id == owner_user_id),
        (ProfileVersionRecord, ProfileVersionRecord.owner_user_id == owner_user_id),
        (JobResultClaimRecord, JobResultClaimRecord.owner_user_id == owner_user_id),
        (
            ObjectUploadRegistrationRecord,
            ObjectUploadRegistrationRecord.owner_user_id == owner_user_id,
        ),
        (ReplayResultRecord, ReplayResultRecord.owner_user_id == owner_user_id),
        (ComparisonResultRecord, ComparisonResultRecord.owner_user_id == owner_user_id),
        (ScenarioRecord, ScenarioRecord.owner_user_id == owner_user_id),
        (ScenarioResultRecord, ScenarioResultRecord.owner_user_id == owner_user_id),
        (DeletionIntentRecord, DeletionIntentRecord.owner_user_id == owner_user_id),
    )
    total = sum(_count(database, model, criterion) for model, criterion in direct)
    total += _count(
        database,
        JobRecord,
        JobRecord.owner_user_id == owner_user_id,
        JobRecord.id != deletion_job_id,
    )
    return total


def _count(
    database: Session,
    model: type[object],
    *criteria: ColumnElement[bool],
) -> int:
    value = database.scalar(select(func.count()).select_from(model).where(*criteria))
    return int(value or 0)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
