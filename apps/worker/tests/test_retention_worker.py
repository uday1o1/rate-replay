from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.backups import BackupRetentionService
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    DeletionAuditRecord,
    DeletionIntentRecord,
    DeletionReceiptRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    ObjectUploadRegistrationRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    RawObjectRecord,
    SessionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore, ObjectStoreError
from ratereplay_persistence.retention import DatabaseRetentionService, RetentionScheduler
from ratereplay_worker.retention_worker import RetentionWorker
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, 9, tzinfo=UTC)
OWNER_ID = "1" * 32
PREPARED_OWNER_ID = "0" * 32
IMPORT_ID = "2" * 32
PROFILE_ID = "3" * 32


@dataclass(frozen=True, slots=True)
class Harness:
    sessions: sessionmaker[Session]
    objects: FilesystemObjectStore
    ledger: FilesystemDeletionLedger
    scheduler: RetentionScheduler
    worker: RetentionWorker


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(tmp_path / "objects")
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    jobs = JobService(sessions)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=OWNER_ID,
                username_canonical="retention-owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
        database.add(
            UserRecord(
                id=PREPARED_OWNER_ID,
                username_canonical="prepared-retention-owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
        database.add(
            ImportRecord(
                id=IMPORT_ID,
                owner_user_id=OWNER_ID,
                state="CONFIRMED",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                adapter="TEST_CANONICAL",
                raw_content_hash="a" * 64,
                created_at=NOW,
                published_at=NOW,
                confirmed_at=NOW,
                profile_version_id=PROFILE_ID,
            )
        )
        database.add(
            ProfileVersionRecord(
                id=PROFILE_ID,
                owner_user_id=OWNER_ID,
                import_id=IMPORT_ID,
                content_hash="b" * 64,
                canonical_content=b"retention-profile",
                billing_period_start_utc_ns=0,
                billing_period_end_utc_ns=1,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=900,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=NOW,
            )
        )
    return Harness(
        sessions=sessions,
        objects=objects,
        ledger=ledger,
        scheduler=RetentionScheduler(sessions),
        worker=RetentionWorker(
            worker_id="retention-worker",
            session_factory=sessions,
            jobs=jobs,
            imports=ImportService(sessions, objects),
            artifacts=ArtifactService(sessions, objects),
            deletions=DeletionSweepService(sessions, objects, ledger),
            database_retention=DatabaseRetentionService(sessions, ledger),
        ),
    )


def test_scheduler_is_deterministic_with_an_hourly_system_scope(harness: Harness) -> None:
    first = harness.scheduler.schedule(now=NOW + timedelta(minutes=12))
    repeated = harness.scheduler.schedule(now=NOW + timedelta(minutes=59, seconds=59))
    following = harness.scheduler.schedule(now=NOW + timedelta(hours=1))

    assert first.job_id == repeated.job_id
    assert not first.repeated and repeated.repeated
    assert following.job_id != first.job_id and not following.repeated
    with harness.sessions() as database:
        job = database.get(JobRecord, first.job_id)
        assert job is not None
        assert (
            job.kind,
            job.scope_mode,
            job.owner_user_id,
            job.import_id,
            job.profile_version_id,
        ) == ("RETENTION", "SYSTEM_SCOPE", None, None, None)
        assert job.not_before == NOW.replace(tzinfo=None)


def test_retention_worker_enforces_exact_expiry_and_preserves_controls(
    harness: Harness,
) -> None:
    expired_raw_key = "raw/expired"
    future_raw_key = "raw/future"
    orphan_key = "artifacts/orphan"
    accepted_key = "artifacts/accepted"
    for key in (expired_raw_key, future_raw_key, orphan_key, accepted_key):
        harness.objects.put_file(key, BytesIO(key.encode()), maximum_bytes=1024)

    _seed_expiry_boundaries(harness, expired_raw_key, future_raw_key)
    _seed_artifacts(harness, orphan_key, accepted_key)
    _seed_deletion_controls(harness)
    old_job_id = _seed_old_retention_job(harness)

    scheduled = harness.scheduler.schedule(now=NOW)
    assert harness.worker.run_once(now=NOW)
    outcome = harness.worker.last_outcome
    assert outcome is not None
    assert (
        outcome.raw_objects,
        outcome.orphan_artifacts,
        outcome.receipt_verifiers,
        outcome.expired_backups,
        outcome.database.expired_operations,
        outcome.database.expired_sessions,
        outcome.database.expired_deletion_intents,
        outcome.database.expired_retention_jobs,
    ) == (1, 1, 1, 0, 1, 1, 1, 1)

    assert not harness.objects.exists(expired_raw_key)
    assert harness.objects.exists(future_raw_key)
    assert not harness.objects.exists(orphan_key)
    assert harness.objects.exists(accepted_key)
    with harness.sessions() as database:
        current = database.get(JobRecord, scheduled.job_id)
        expired_raw = database.get(RawObjectRecord, "4" * 32)
        future_raw = database.get(RawObjectRecord, "5" * 32)
        orphan = database.get(ObjectUploadRegistrationRecord, "6" * 32)
        accepted = database.get(ObjectUploadRegistrationRecord, "7" * 32)
        expired_audit = database.get(DeletionAuditRecord, "e" * 32)
        assert current is not None and current.state == "SUCCEEDED"
        assert expired_raw is not None and expired_raw.state == "DELETED"
        assert future_raw is not None and future_raw.state == "AVAILABLE"
        assert orphan is not None and orphan.state == "DELETED"
        assert accepted is not None and accepted.state == "ACCEPTED"
        assert database.get(OperationRequestRecord, "8" * 32) is None
        assert database.get(OperationRequestRecord, "9" * 32) is not None
        assert database.get(OperationRequestRecord, "0" * 32) is not None
        assert database.get(OperationRequestRecord, "q" * 32) is not None
        assert database.get(SessionRecord, "a" * 32) is None
        assert database.get(SessionRecord, "b" * 32) is not None
        assert database.get(DeletionIntentRecord, "c" * 32) is None
        assert database.get(DeletionReceiptRecord, "c" * 32) is None
        assert database.get(DeletionIntentRecord, "d" * 32) is not None
        assert database.get(DeletionReceiptRecord, "d" * 32) is not None
        assert database.get(DeletionReceiptRecord, "e" * 32) is None
        assert expired_audit is not None and expired_audit.receipt_verifier is None
        assert database.get(DeletionReceiptRecord, "f" * 32) is not None
        assert database.get(JobRecord, old_job_id) is None


def test_tampered_request_fails_closed_before_any_expiry(harness: Harness) -> None:
    raw_key = "raw/tampered-control"
    harness.objects.put_file(raw_key, BytesIO(b"raw"), maximum_bytes=1024)
    _seed_raw(harness, record_id="4" * 32, import_id="4i" * 16, key=raw_key, expires_at=NOW)
    scheduled = harness.scheduler.schedule(now=NOW)
    with harness.sessions.begin() as database:
        job = database.get(JobRecord, scheduled.job_id)
        assert job is not None
        job.request_json = "{}"

    assert harness.worker.run_once(now=NOW)
    assert harness.worker.last_outcome is None
    assert harness.objects.exists(raw_key)
    with harness.sessions() as database:
        job = database.get(JobRecord, scheduled.job_id)
        raw = database.get(RawObjectRecord, "4" * 32)
        assert job is not None and job.state == "FAILED"
        assert job.failure_code == "RETENTION_REQUEST_INVALID"
        assert raw is not None and raw.state == "AVAILABLE"


def test_durable_retention_job_expires_encrypted_backup_prefix(
    harness: Harness,
    tmp_path: Path,
) -> None:
    backup_objects = FilesystemObjectStore(tmp_path / "backups")
    backup_id = "20260715T090000000000Z-0123456789abcdef"
    backup_objects.put_file(
        f"backups/{backup_id}/database.dump",
        BytesIO(b"expired backup"),
        maximum_bytes=1024,
    )
    harness.worker._backup_retention = BackupRetentionService(backup_objects)
    harness.scheduler.schedule(now=NOW)

    assert harness.worker.run_once(now=NOW)

    assert harness.worker.last_outcome is not None
    assert harness.worker.last_outcome.expired_backups == 1
    assert backup_objects.list_prefix(f"backups/{backup_id}") == ()


def test_raw_deadline_job_runs_at_the_exact_ttl_boundary(harness: Harness) -> None:
    raw_key = "raw/exact-deadline"
    deadline = NOW + timedelta(seconds=37)
    harness.objects.put_file(raw_key, BytesIO(b"raw"), maximum_bytes=1024)
    _seed_raw(
        harness,
        record_id="4" * 32,
        import_id="4i" * 16,
        key=raw_key,
        expires_at=deadline,
    )

    scheduled = harness.scheduler.schedule_raw_expirations(now=NOW)
    repeated = harness.scheduler.schedule_raw_expirations(now=NOW + timedelta(seconds=1))

    assert len(scheduled) == 1 and scheduled[0].scheduled_for == deadline
    assert len(repeated) == 1 and repeated[0].job_id == scheduled[0].job_id
    assert repeated[0].repeated
    assert not harness.worker.run_once(now=deadline - timedelta(microseconds=1))
    assert harness.objects.exists(raw_key)
    assert harness.worker.run_once(now=deadline)
    assert not harness.objects.exists(raw_key)
    with harness.sessions() as database:
        job = database.get(JobRecord, scheduled[0].job_id)
        assert job is not None and job.state == "SUCCEEDED"


def test_object_delete_failure_retries_without_marking_raw_deleted(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "raw/delete-failure"
    harness.objects.put_file(raw_key, BytesIO(b"raw"), maximum_bytes=1024)
    _seed_raw(harness, record_id="4" * 32, import_id="4i" * 16, key=raw_key, expires_at=NOW)
    scheduled = harness.scheduler.schedule(now=NOW)

    def fail_delete(_key: str) -> None:
        raise ObjectStoreError("OBJECT_DELETE_FAILED", "injected deletion failure")

    monkeypatch.setattr(harness.objects, "delete", fail_delete)
    assert harness.worker.run_once(now=NOW)
    with harness.sessions() as database:
        job = database.get(JobRecord, scheduled.job_id)
        raw = database.get(RawObjectRecord, "4" * 32)
        assert job is not None and job.state == "QUEUED"
        assert job.completed_at is None
        assert raw is not None and raw.state == "DELETE_PENDING"


def test_database_failure_retries_the_fenced_retention_job(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled = harness.scheduler.schedule(now=NOW)

    def fail_database(*, current_job_id: str, now: datetime) -> object:
        raise SQLAlchemyError(f"injected failure for {current_job_id} at {now.isoformat()}")

    monkeypatch.setattr(harness.worker._database, "expire", fail_database)
    assert harness.worker.run_once(now=NOW)
    assert harness.worker.last_outcome is None
    with harness.sessions() as database:
        job = database.get(JobRecord, scheduled.job_id)
        assert job is not None and job.state == "QUEUED"
        assert job.completed_at is None


def _seed_expiry_boundaries(
    harness: Harness,
    expired_raw_key: str,
    future_raw_key: str,
) -> None:
    _seed_raw(
        harness,
        record_id="4" * 32,
        import_id="4i" * 16,
        key=expired_raw_key,
        expires_at=NOW,
    )
    _seed_raw(
        harness,
        record_id="5" * 32,
        import_id="5i" * 16,
        key=future_raw_key,
        expires_at=NOW + timedelta(microseconds=1),
    )
    with harness.sessions.begin() as database:
        database.add_all(
            (
                OperationRequestRecord(
                    id="8" * 32,
                    owner_user_id=OWNER_ID,
                    route_id="POST:/v1/imports",
                    idempotency_key="expired-operation",
                    request_schema_version="import-request-v1",
                    canonical_payload_hash="1" * 64,
                    operation_id="5i" * 16,
                    created_at=NOW - timedelta(days=1),
                    expires_at=NOW,
                ),
                OperationRequestRecord(
                    id="9" * 32,
                    owner_user_id=OWNER_ID,
                    route_id="POST:/v1/imports",
                    idempotency_key="future-operation",
                    request_schema_version="import-request-v1",
                    canonical_payload_hash="2" * 64,
                    operation_id="5i" * 16,
                    created_at=NOW,
                    expires_at=NOW + timedelta(microseconds=1),
                ),
                OperationRequestRecord(
                    id="0" * 32,
                    owner_user_id=OWNER_ID,
                    route_id="POST:/v1/reports/{scenario_id}/exports",
                    idempotency_key="nonterminal-operation",
                    request_schema_version="report-operation-v1",
                    canonical_payload_hash="0" * 64,
                    operation_id="0j" * 16,
                    created_at=NOW - timedelta(days=1),
                    expires_at=NOW,
                ),
                OperationRequestRecord(
                    id="q" * 32,
                    owner_user_id=OWNER_ID,
                    route_id="POST:/v1/imports",
                    idempotency_key="nonterminal-import-operation",
                    request_schema_version="import-request-v1",
                    canonical_payload_hash="q" * 64,
                    operation_id="4i" * 16,
                    created_at=NOW - timedelta(days=1),
                    expires_at=NOW,
                ),
                JobRecord(
                    id="qi" * 16,
                    owner_user_id=OWNER_ID,
                    kind="IMPORT",
                    request_schema_version="import-request-v1",
                    request_hash="q" * 64,
                    request_json="{}",
                    scope_mode="ACTIVE_SCOPE",
                    import_id="4i" * 16,
                    profile_version_id=None,
                    captured_account_generation=0,
                    captured_import_generation=0,
                    captured_profile_generation=None,
                    state="QUEUED",
                    attempt_count=0,
                    max_attempts=3,
                    fencing_generation=0,
                    not_before=NOW,
                    cancel_requested=False,
                    created_at=NOW - timedelta(days=1),
                ),
                SessionRecord(
                    id="a" * 32,
                    user_id=OWNER_ID,
                    token_hash="3" * 64,
                    csrf_hash="4" * 64,
                    created_at=NOW - timedelta(hours=1),
                    last_seen_at=NOW - timedelta(hours=1),
                    idle_expires_at=NOW,
                    absolute_expires_at=NOW + timedelta(days=1),
                ),
                SessionRecord(
                    id="b" * 32,
                    user_id=OWNER_ID,
                    token_hash="5" * 64,
                    csrf_hash="6" * 64,
                    created_at=NOW,
                    last_seen_at=NOW,
                    idle_expires_at=NOW + timedelta(microseconds=1),
                    absolute_expires_at=NOW + timedelta(days=1),
                ),
            )
        )


def _seed_raw(
    harness: Harness,
    *,
    record_id: str,
    import_id: str,
    key: str,
    expires_at: datetime,
) -> None:
    with harness.sessions.begin() as database:
        database.add(
            ImportRecord(
                id=import_id,
                owner_user_id=OWNER_ID,
                state="READY",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                adapter="PGE_CSV",
                raw_content_hash=record_id[0] * 64,
                created_at=NOW - timedelta(days=1),
            )
        )
        database.add(
            RawObjectRecord(
                id=record_id,
                owner_user_id=OWNER_ID,
                import_id=import_id,
                object_key=key,
                content_hash=record_id[0] * 64,
                size_bytes=3,
                state="AVAILABLE",
                created_at=NOW - timedelta(days=1),
                expires_at=expires_at,
            )
        )


def _seed_artifacts(harness: Harness, orphan_key: str, accepted_key: str) -> None:
    with harness.sessions.begin() as database:
        database.add_all(
            (
                _report_job("6j" * 16, state="FAILED"),
                _report_job("7j" * 16, state="SUCCEEDED"),
                _report_job("0j" * 16, state="QUEUED"),
                ObjectUploadRegistrationRecord(
                    id="6" * 32,
                    owner_user_id=OWNER_ID,
                    job_id="6j" * 16,
                    attempt_number=1,
                    fencing_generation=1,
                    artifact_class="REPORT",
                    object_key=orphan_key,
                    upload_identifier="6u" * 16,
                    state="STAGED",
                    content_hash="6" * 64,
                    size_bytes=len(orphan_key),
                    created_at=NOW - timedelta(minutes=6),
                    updated_at=NOW - timedelta(minutes=5),
                ),
                ObjectUploadRegistrationRecord(
                    id="7" * 32,
                    owner_user_id=OWNER_ID,
                    job_id="7j" * 16,
                    attempt_number=1,
                    fencing_generation=1,
                    artifact_class="REPORT",
                    object_key=accepted_key,
                    upload_identifier="7u" * 16,
                    state="ACCEPTED",
                    content_hash="7" * 64,
                    size_bytes=len(accepted_key),
                    created_at=NOW - timedelta(minutes=6),
                    updated_at=NOW - timedelta(minutes=5),
                    accepted_at=NOW - timedelta(minutes=5),
                ),
            )
        )


def _report_job(job_id: str, *, state: str) -> JobRecord:
    return JobRecord(
        id=job_id,
        owner_user_id=OWNER_ID,
        kind="REPORT",
        request_schema_version="report-request-v1",
        request_hash=job_id[0] * 64,
        request_json="{}",
        scope_mode="ACTIVE_SCOPE",
        import_id=IMPORT_ID,
        profile_version_id=PROFILE_ID,
        captured_account_generation=0,
        captured_import_generation=0,
        captured_profile_generation=0,
        state=state,
        attempt_count=1,
        max_attempts=3,
        fencing_generation=1,
        not_before=NOW,
        cancel_requested=False,
        created_at=NOW - timedelta(minutes=6),
        completed_at=(
            NOW - timedelta(minutes=5) if state in {"SUCCEEDED", "FAILED", "CANCELLED"} else None
        ),
    )


def _seed_deletion_controls(harness: Harness) -> None:
    expired_id = "c" * 32
    prepared_id = "d" * 32
    expired_receipt_id = "e" * 32
    future_receipt_id = "f" * 32
    with harness.sessions.begin() as database:
        for deletion_id, key in ((expired_id, "expired"), (prepared_id, "prepared")):
            database.add(
                DeletionIntentRecord(
                    deletion_id=deletion_id,
                    owner_user_id=(OWNER_ID if deletion_id == expired_id else PREPARED_OWNER_ID),
                    idempotency_key=key,
                    request_schema_version="account-deletion-intent-v1",
                    canonical_payload_hash=deletion_id[0] * 64,
                    receipt_digest=(deletion_id[0].upper()) * 64,
                    target_scope_id=(deletion_id[0] + "s") * 16,
                    original_generation=0,
                    proposed_generation=1,
                    state="INTENT_CREATED",
                    created_at=NOW - timedelta(minutes=15),
                    expires_at=NOW,
                )
            )
            database.add(
                DeletionReceiptRecord(
                    deletion_id=deletion_id,
                    receipt_verifier=f"verifier-{key}",
                    status="INTENT_CREATED",
                    artifact_counts_json="{}",
                    created_at=NOW - timedelta(minutes=15),
                )
            )
        for deletion_id, expires_at in (
            (expired_receipt_id, NOW),
            (future_receipt_id, NOW + timedelta(microseconds=1)),
        ):
            database.add(
                DeletionReceiptRecord(
                    deletion_id=deletion_id,
                    receipt_verifier=f"verifier-{deletion_id[0]}",
                    status="DELETED",
                    artifact_counts_json="{}",
                    created_at=NOW - timedelta(days=1),
                    completed_at=NOW - timedelta(hours=1),
                    verifier_expires_at=expires_at,
                )
            )
            database.add(
                DeletionAuditRecord(
                    deletion_id=deletion_id,
                    receipt_verifier=f"verifier-{deletion_id[0]}",
                    verifier_expires_at=expires_at,
                    scope_token=deletion_id[0] * 64,
                    restore_key_version="restore-key-v1",
                    deletion_generation=1,
                    completed_at=NOW - timedelta(hours=1),
                    artifact_counts_json="{}",
                    status="DELETED",
                    status_code="DELETION_COMPLETE",
                )
            )
    harness.ledger.append(
        deletion_id=prepared_id,
        phase="PREPARED",
        scope_token="d" * 64,
        restore_key_version="restore-key-v1",
        original_generation=0,
        proposed_generation=1,
        preparation_digest="1" * 64,
        intent_proof_digest="2" * 64,
        occurred_at=NOW - timedelta(seconds=1),
    )


def _seed_old_retention_job(harness: Harness) -> str:
    job_id = "0" * 32
    with harness.sessions.begin() as database:
        database.add(
            JobRecord(
                id=job_id,
                owner_user_id=None,
                kind="RETENTION",
                request_schema_version="retention-sweep-v1",
                request_hash="0" * 64,
                request_json="{}",
                scope_mode="SYSTEM_SCOPE",
                captured_account_generation=0,
                state="SUCCEEDED",
                attempt_count=1,
                max_attempts=5,
                fencing_generation=1,
                not_before=NOW - timedelta(days=7),
                cancel_requested=False,
                created_at=NOW - timedelta(days=7),
                completed_at=NOW - timedelta(days=7),
            )
        )
        database.add(
            JobAttemptRecord(
                id="0a" * 16,
                job_id=job_id,
                attempt_number=1,
                fencing_generation=1,
                worker_id="old-retention-worker",
                state="SUCCEEDED",
                leased_at=NOW - timedelta(days=7),
                lease_expires_at=NOW - timedelta(days=7) + timedelta(seconds=20),
                completed_at=NOW - timedelta(days=7),
            )
        )
    return job_id
