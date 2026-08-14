from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.deletions import DeletionCoordinator
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    DeletionAuditRecord,
    DeletionControlOperationRecord,
    DeletionIntentRecord,
    DeletionLedgerReceiptRecord,
    DeletionReceiptRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    RawObjectRecord,
    SessionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.retention import DatabaseRetentionService, RetentionScheduler
from ratereplay_worker.retention_worker import RetentionWorker
from sqlalchemy import delete, select

pytestmark = pytest.mark.postgres


def test_postgres_retention_serializes_with_deletion_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(tmp_path / "objects")
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    coordinator = DeletionCoordinator(sessions, ledger, restore_key=b"r" * 32)
    database_retention = DatabaseRetentionService(sessions, ledger)
    now = datetime.now(UTC).replace(microsecond=0)
    expiry = now + timedelta(minutes=15)
    owner_id = secrets.token_hex(16)
    import_id = secrets.token_hex(16)
    raw_id = secrets.token_hex(16)
    operation_id = secrets.token_hex(16)
    session_id = secrets.token_hex(16)
    receipt_secret = b"s" * 32
    raw_key = f"raw/{raw_id}"
    deletion_id = ""
    retention_job_id = ""
    prepared_append_entered = Event()
    release_prepared_append = Event()
    objects.put_file(raw_key, BytesIO(b"postgres-retention"), maximum_bytes=1024)
    try:
        with sessions.begin() as database:
            database.add(
                UserRecord(
                    id=owner_id,
                    username_canonical=f"retention_{owner_id}",
                    password_hash="test-only",
                    created_at=now,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                )
            )
            database.flush()
            database.add(
                ImportRecord(
                    id=import_id,
                    owner_user_id=owner_id,
                    state="READY",
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    adapter="PGE_CSV",
                    raw_content_hash="a" * 64,
                    created_at=now - timedelta(days=1),
                )
            )
            database.flush()
            database.add(
                RawObjectRecord(
                    id=raw_id,
                    owner_user_id=owner_id,
                    import_id=import_id,
                    object_key=raw_key,
                    content_hash="b" * 64,
                    size_bytes=len(b"postgres-retention"),
                    state="AVAILABLE",
                    created_at=now - timedelta(days=1),
                    expires_at=expiry,
                )
            )
            database.add(
                OperationRequestRecord(
                    id=operation_id,
                    owner_user_id=owner_id,
                    route_id="POST:/v1/imports",
                    idempotency_key="postgres-retention-operation",
                    request_schema_version="import-request-v1",
                    canonical_payload_hash="c" * 64,
                    operation_id=import_id,
                    created_at=now - timedelta(days=1),
                    expires_at=expiry,
                )
            )
            database.add(
                SessionRecord(
                    id=session_id,
                    user_id=owner_id,
                    token_hash="d" * 64,
                    csrf_hash="e" * 64,
                    created_at=now,
                    last_seen_at=now,
                    idle_expires_at=expiry,
                    absolute_expires_at=expiry + timedelta(days=1),
                )
            )
        deletion_id = coordinator.create_intent(
            owner_user_id=owner_id,
            idempotency_key="postgres-retention-deletion",
            receipt_secret=receipt_secret,
            now=now,
        ).deletion_id

        original_append = ledger.append

        def controlled_append(**arguments: object):  # type: ignore[no-untyped-def]
            if arguments.get("phase") == "PREPARED":
                prepared_append_entered.set()
                if not release_prepared_append.wait(timeout=10):
                    raise AssertionError("retention race did not release the ledger append")
            return original_append(**arguments)  # type: ignore[arg-type]

        monkeypatch.setattr(ledger, "append", controlled_append)
        with ThreadPoolExecutor(max_workers=2) as executor:
            deletion_future = executor.submit(
                coordinator.authorize_and_start,
                owner_user_id=owner_id,
                deletion_id=deletion_id,
                receipt_secret=receipt_secret,
                now=expiry - timedelta(seconds=1),
            )
            assert prepared_append_entered.wait(timeout=10)
            retention_future = executor.submit(
                database_retention.expire,
                current_job_id="not-a-retention-job",
                now=expiry,
            )
            with pytest.raises(TimeoutError):
                retention_future.result(timeout=0.5)
            release_prepared_append.set()
            assert deletion_future.result(timeout=10).status == "DELETING"
            race_outcome = retention_future.result(timeout=10)

        assert race_outcome.expired_deletion_intents == 0
        assert tuple(event.phase for event in ledger.chain(deletion_id)) == (
            "PREPARED",
            "REQUESTED",
        )
        with sessions() as database:
            intent = database.get(DeletionIntentRecord, deletion_id)
            assert intent is not None and intent.state == "CONSUMED"

        scheduler = RetentionScheduler(sessions)
        scheduled_deadlines = scheduler.schedule_raw_expirations(now=now)
        repeated_deadlines = scheduler.schedule_raw_expirations(now=now + timedelta(seconds=1))
        assert len(scheduled_deadlines) == 1 and len(repeated_deadlines) == 1
        scheduled = scheduled_deadlines[0]
        repeated = repeated_deadlines[0]
        retention_job_id = scheduled.job_id
        assert scheduled.scheduled_for == expiry
        assert repeated.job_id == scheduled.job_id and repeated.repeated
        worker = RetentionWorker(
            worker_id="postgres-retention-worker",
            session_factory=sessions,
            jobs=JobService(sessions),
            imports=ImportService(sessions, objects),
            artifacts=ArtifactService(sessions, objects),
            deletions=DeletionSweepService(sessions, objects, ledger),
            database_retention=database_retention,
        )
        assert worker.run_once(now=expiry)
        assert not objects.exists(raw_key)
        with sessions() as database:
            raw = database.get(RawObjectRecord, raw_id)
            job = database.get(JobRecord, retention_job_id)
            assert raw is not None and raw.state == "DELETED"
            assert job is not None and job.state == "SUCCEEDED"
            assert database.get(OperationRequestRecord, operation_id) is None
            assert database.get(SessionRecord, session_id) is None
            assert database.get(DeletionIntentRecord, deletion_id) is not None
    finally:
        release_prepared_append.set()
        with sessions.begin() as database:
            database.execute(
                delete(DeletionControlOperationRecord).where(
                    DeletionControlOperationRecord.deletion_id == deletion_id
                )
            )
            job_ids = database.scalars(
                select(JobRecord.id).where(
                    (JobRecord.owner_user_id == owner_id) | (JobRecord.id == retention_job_id)
                )
            ).all()
            if job_ids:
                database.execute(
                    delete(JobAttemptRecord).where(JobAttemptRecord.job_id.in_(job_ids))
                )
                database.execute(delete(JobRecord).where(JobRecord.id.in_(job_ids)))
            database.execute(
                delete(DeletionLedgerReceiptRecord).where(
                    DeletionLedgerReceiptRecord.deletion_id == deletion_id
                )
            )
            database.execute(
                delete(DeletionIntentRecord).where(DeletionIntentRecord.deletion_id == deletion_id)
            )
            database.execute(
                delete(DeletionReceiptRecord).where(
                    DeletionReceiptRecord.deletion_id == deletion_id
                )
            )
            database.execute(
                delete(DeletionAuditRecord).where(DeletionAuditRecord.deletion_id == deletion_id)
            )
            database.execute(
                delete(OperationRequestRecord).where(
                    OperationRequestRecord.owner_user_id == owner_id
                )
            )
            database.execute(delete(SessionRecord).where(SessionRecord.user_id == owner_id))
            database.execute(
                delete(RawObjectRecord).where(RawObjectRecord.owner_user_id == owner_id)
            )
            database.execute(delete(ImportRecord).where(ImportRecord.owner_user_id == owner_id))
            database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
        engine.dispose()
