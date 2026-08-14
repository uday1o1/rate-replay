from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.deletions import DeletionCoordinator, DeletionServiceError
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
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
    ObjectUploadRegistrationRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    RawObjectRecord,
    SessionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_worker.deletion_worker import DeletionWorker
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
OWNER_ID = "1" * 32
IMPORT_ID = "2" * 32
PROFILE_ID = "3" * 32
ORDINARY_JOB_ID = "ordinary-report-job"
UPLOAD_ID = "4" * 32
SECRET = b"s" * 32


@dataclass(frozen=True, slots=True)
class Harness:
    sessions: sessionmaker[Session]
    objects: FilesystemObjectStore
    ledger: FilesystemDeletionLedger
    coordinator: DeletionCoordinator
    jobs: JobService
    sweeps: DeletionSweepService
    worker: DeletionWorker


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(tmp_path / "objects")
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    coordinator = DeletionCoordinator(sessions, ledger, restore_key=b"r" * 32)
    jobs = JobService(sessions)
    sweeps = DeletionSweepService(sessions, objects, ledger)
    worker = DeletionWorker(worker_id="deletion-worker", jobs=jobs, sweeps=sweeps)
    _seed_owner_data(sessions, objects)
    return Harness(sessions, objects, ledger, coordinator, jobs, sweeps, worker)


def _seed_owner_data(
    sessions: sessionmaker[Session],
    objects: FilesystemObjectStore,
) -> None:
    raw_key = f"owners/{OWNER_ID}/imports/{IMPORT_ID}/raw"
    upload_key = f"owners/{OWNER_ID}/jobs/{ORDINARY_JOB_ID}/attempts/0/REPORT"
    objects.put_file(raw_key, BytesIO(b"raw interval data"), maximum_bytes=1024)
    objects.put_file(upload_key, BytesIO(b"staged report"), maximum_bytes=1024)
    objects.put_file(
        f"owners/{OWNER_ID}/unregistered-crash.partial",
        BytesIO(b"orphaned partial"),
        maximum_bytes=1024,
    )
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=OWNER_ID,
                username_canonical="sweep-owner",
                password_hash="private-password-hash",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
        database.add(
            SessionRecord(
                id="5" * 32,
                user_id=OWNER_ID,
                token_hash="a" * 64,
                csrf_hash="b" * 64,
                created_at=NOW,
                last_seen_at=NOW,
                idle_expires_at=NOW + timedelta(hours=1),
                absolute_expires_at=NOW + timedelta(days=1),
            )
        )
        database.add(
            OperationRequestRecord(
                id="6" * 32,
                owner_user_id=OWNER_ID,
                route_id="report",
                idempotency_key="private-idempotency-key",
                request_schema_version="report-v1",
                canonical_payload_hash="c" * 64,
                operation_id="7" * 32,
                created_at=NOW,
                expires_at=NOW + timedelta(days=1),
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
                raw_content_hash="d" * 64,
                created_at=NOW,
                published_at=NOW,
                confirmed_at=NOW,
                profile_version_id=PROFILE_ID,
            )
        )
        database.flush()
        database.add(
            RawObjectRecord(
                id="8" * 32,
                owner_user_id=OWNER_ID,
                import_id=IMPORT_ID,
                object_key=raw_key,
                content_hash="e" * 64,
                size_bytes=17,
                state="AVAILABLE",
                created_at=NOW,
                expires_at=NOW + timedelta(days=1),
            )
        )
        database.add(
            ImportReadingRecord(
                id="9" * 32,
                import_id=IMPORT_ID,
                start_utc_ns=0,
                duration_seconds=900,
                energy_wh=100,
                flow_direction="IMPORT",
                source_unit="Wh",
                source_multiplier=0,
                source_reading_type="TEST",
                source_service_category="ELECTRICITY",
                source_commodity="ELECTRICITY",
                source_accumulation_behavior="DELTA_DATA",
                source_data_qualifier="REGULAR",
                source_time_attribute="CLOCK",
                quality_flags_json="[]",
            )
        )
        database.add(
            ImportFindingRecord(
                id="a" * 32,
                import_id=IMPORT_ID,
                code="TEST_FINDING",
                severity="WARNING",
                field_path="interval",
                safe_value="present",
            )
        )
        database.add(
            ProfileVersionRecord(
                id=PROFILE_ID,
                owner_user_id=OWNER_ID,
                import_id=IMPORT_ID,
                content_hash="f" * 64,
                canonical_content=b"private canonical profile",
                billing_period_start_utc_ns=0,
                billing_period_end_utc_ns=1,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=900,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=NOW,
            )
        )
        database.add(
            JobRecord(
                id=ORDINARY_JOB_ID,
                owner_user_id=OWNER_ID,
                kind="REPORT",
                request_schema_version="report-v1",
                request_hash="0" * 64,
                scope_mode="ACTIVE_SCOPE",
                request_json='{"private":"report input"}',
                import_id=IMPORT_ID,
                profile_version_id=PROFILE_ID,
                captured_account_generation=0,
                captured_import_generation=0,
                captured_profile_generation=0,
                state="QUEUED",
                attempt_count=0,
                max_attempts=3,
                fencing_generation=0,
                not_before=NOW,
                cancel_requested=False,
                created_at=NOW,
            )
        )
        database.add(
            ObjectUploadRegistrationRecord(
                id=UPLOAD_ID,
                owner_user_id=OWNER_ID,
                job_id=ORDINARY_JOB_ID,
                attempt_number=0,
                fencing_generation=0,
                artifact_class="REPORT",
                object_key=upload_key,
                upload_identifier="b" * 32,
                state="STAGED",
                content_hash="1" * 64,
                size_bytes=13,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def _start(harness: Harness) -> str:
    intent = harness.coordinator.create_intent(
        owner_user_id=OWNER_ID,
        idempotency_key="delete-owner",
        receipt_secret=SECRET,
        now=NOW,
    )
    harness.coordinator.authorize_and_start(
        owner_user_id=OWNER_ID,
        deletion_id=intent.deletion_id,
        receipt_secret=SECRET,
        now=NOW + timedelta(seconds=1),
    )
    return intent.deletion_id


def _lease_deletion(harness: Harness, *, now: datetime) -> JobLease:
    lease = harness.jobs.lease_next(
        worker_id="manual-deletion-worker",
        now=now,
        kinds=frozenset({"DELETION"}),
    )
    assert lease is not None and harness.jobs.start(lease, now=now)
    return lease


def test_worker_sweeps_all_owner_state_and_publishes_minimum_tombstone(
    harness: Harness,
) -> None:
    deletion_id = _start(harness)
    assert harness.worker.run_once(now=NOW + timedelta(seconds=2))
    assert tuple(event.phase for event in harness.ledger.chain(deletion_id)) == (
        "PREPARED",
        "REQUESTED",
        "COMPLETED",
    )
    assert harness.objects.list_prefix(f"owners/{OWNER_ID}") == ()
    status = harness.coordinator.status(
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW + timedelta(seconds=2),
    )
    assert status.status == "DELETED"
    assert status.artifact_counts["imports"] == 1
    assert status.artifact_counts["objects"] == 2
    assert status.artifact_counts["object_uploads"] == 1
    with harness.sessions() as database:
        assert database.get(UserRecord, OWNER_ID) is None
        assert database.get(DeletionControlOperationRecord, deletion_id) is None
        assert database.get(DeletionIntentRecord, deletion_id) is None
        assert database.scalar(select(func.count()).select_from(JobRecord)) == 0
        assert database.scalar(select(func.count()).select_from(JobAttemptRecord)) == 0
        assert database.scalar(select(func.count()).select_from(DeletionFenceTargetRecord)) == 0
        assert database.scalar(select(func.count()).select_from(DeletionLedgerReceiptRecord)) == 0
        receipt = database.get(DeletionReceiptRecord, deletion_id)
        audit = database.get(DeletionAuditRecord, deletion_id)
        assert receipt is not None and receipt.status == "DELETED"
        assert audit is not None and audit.status_code == "VERIFIED_COMPLETE"
        tombstone_text = " ".join(str(value) for value in audit.__dict__.values())
        assert "sweep-owner" not in tombstone_text
        assert "private" not in tombstone_text

    expiry = NOW + timedelta(seconds=2, days=30)
    assert harness.sweeps.expire_receipt_verifiers(now=expiry) == 1
    with harness.sessions() as database:
        assert database.get(DeletionReceiptRecord, deletion_id) is None
        audit = database.get(DeletionAuditRecord, deletion_id)
        assert audit is not None and audit.receipt_verifier is None
    with pytest.raises(DeletionServiceError) as raised:
        harness.coordinator.status(
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=expiry,
        )
    assert raised.value.code == "DELETION_RECEIPT_EXPIRED"


def test_drain_waits_for_live_writer_and_registered_upload(harness: Harness) -> None:
    with harness.sessions.begin() as database:
        ordinary = database.get(JobRecord, ORDINARY_JOB_ID)
        upload = database.get(ObjectUploadRegistrationRecord, UPLOAD_ID)
        assert ordinary is not None and upload is not None
        ordinary.state = "RUNNING"
        ordinary.attempt_count = 1
        ordinary.fencing_generation = 1
        ordinary.lease_owner = "ordinary-worker"
        ordinary.lease_acquired_at = NOW
        ordinary.lease_expires_at = NOW + timedelta(seconds=15)
        ordinary.heartbeat_at = NOW
        upload.state = "REGISTERED"
        upload.attempt_number = 1
        upload.fencing_generation = 1
        database.add(
            JobAttemptRecord(
                id="c" * 32,
                job_id=ORDINARY_JOB_ID,
                attempt_number=1,
                fencing_generation=1,
                worker_id="ordinary-worker",
                state="RUNNING",
                leased_at=NOW,
                lease_expires_at=NOW + timedelta(seconds=15),
            )
        )
    deletion_id = _start(harness)
    assert harness.worker.run_once(now=NOW + timedelta(seconds=2))
    with harness.sessions() as database:
        control = database.get(DeletionControlOperationRecord, deletion_id)
        assert control is not None and control.phase == "DRAIN"
        assert (
            database.scalar(
                select(func.count())
                .select_from(DeletionFenceTargetRecord)
                .where(DeletionFenceTargetRecord.deletion_id == deletion_id)
            )
            == 2
        )
    assert (
        harness.coordinator.status(
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=NOW + timedelta(seconds=2),
        ).status
        == "DELETING"
    )
    assert harness.worker.run_once(now=NOW + timedelta(seconds=25))
    assert (
        harness.coordinator.status(
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=NOW + timedelta(seconds=25),
        ).status
        == "DELETED"
    )


def test_crash_after_sweep_preserves_control_for_verify_resume(harness: Harness) -> None:
    deletion_id = _start(harness)
    lease = _lease_deletion(harness, now=NOW + timedelta(seconds=2))
    assert harness.sweeps.advance(lease, now=NOW + timedelta(seconds=2)).phase == "SWEEP"
    assert harness.sweeps.advance(lease, now=NOW + timedelta(seconds=2)).phase == "VERIFY"
    with harness.sessions() as database:
        control = database.get(DeletionControlOperationRecord, deletion_id)
        assert control is not None and control.phase == "VERIFY"
        assert database.get(UserRecord, OWNER_ID) is not None
        assert database.get(DeletionReceiptRecord, deletion_id) is not None
    assert harness.jobs.rescue_expired(now=NOW + timedelta(seconds=23)) == 1
    assert harness.worker.run_once(now=NOW + timedelta(seconds=23))
    assert (
        harness.coordinator.status(
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=NOW + timedelta(seconds=23),
        ).status
        == "DELETED"
    )


def test_completed_ledger_append_precedes_atomic_terminal_visibility(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deletion_id = _start(harness)
    lease = _lease_deletion(harness, now=NOW + timedelta(seconds=2))
    assert harness.sweeps.advance(lease, now=NOW + timedelta(seconds=2)).phase == "SWEEP"
    assert harness.sweeps.advance(lease, now=NOW + timedelta(seconds=2)).phase == "VERIFY"
    assert harness.sweeps.advance(lease, now=NOW + timedelta(seconds=2)).phase == "COMPLETE"
    real_store = DeletionCoordinator._store_ledger_receipt

    def fail_terminal_store(_database: Session, _event: object) -> None:
        raise DeletionServiceError("TEST_TERMINAL_FAILURE", "injected terminal failure")

    monkeypatch.setattr(DeletionCoordinator, "_store_ledger_receipt", fail_terminal_store)
    with pytest.raises(DeletionServiceError, match="injected terminal failure"):
        harness.sweeps.advance(lease, now=NOW + timedelta(seconds=2))
    assert harness.ledger.chain(deletion_id)[-1].phase == "COMPLETED"
    assert (
        harness.coordinator.status(
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=NOW + timedelta(seconds=2),
        ).status
        == "COMPLETE"
    )
    with harness.sessions() as database:
        assert database.get(DeletionControlOperationRecord, deletion_id) is not None
        assert database.get(UserRecord, OWNER_ID) is not None

    monkeypatch.setattr(DeletionCoordinator, "_store_ledger_receipt", real_store)
    assert harness.sweeps.advance(lease, now=NOW + timedelta(seconds=2)).state == "COMPLETED"
    assert (
        harness.coordinator.status(
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=NOW + timedelta(seconds=2),
        ).status
        == "DELETED"
    )
