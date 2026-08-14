from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.deletions import DeletionCoordinator
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    DeletionAuditRecord,
    DeletionControlOperationRecord,
    DeletionIntentRecord,
    DeletionLedgerReceiptRecord,
    DeletionReceiptRecord,
    JobAttemptRecord,
    JobRecord,
    SessionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_worker.deletion_worker import DeletionWorker
from sqlalchemy import delete, func, select

pytestmark = pytest.mark.postgres


def test_postgres_serializes_intent_and_deletion_start_races(tmp_path: Path) -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    coordinator = DeletionCoordinator(sessions, ledger, restore_key=b"r" * 32)
    now = datetime.now(UTC)
    owner_id = secrets.token_hex(16)
    secret = b"s" * 32
    deletion_id = ""
    try:
        with sessions.begin() as database:
            database.add(
                UserRecord(
                    id=owner_id,
                    username_canonical=f"deletion_{owner_id}",
                    password_hash="test-only",
                    created_at=now,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                )
            )

        def create_intent(_worker: int) -> str:
            return coordinator.create_intent(
                owner_user_id=owner_id,
                idempotency_key="concurrent-intent",
                receipt_secret=secret,
                now=now,
            ).deletion_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            deletion_ids = tuple(executor.map(create_intent, (1, 2)))
        assert len(set(deletion_ids)) == 1
        deletion_id = deletion_ids[0]

        def start_deletion(_worker: int) -> str:
            return coordinator.authorize_and_start(
                owner_user_id=owner_id,
                deletion_id=deletion_id,
                receipt_secret=secret,
                now=now,
            ).status

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = tuple(executor.map(start_deletion, (1, 2)))
        assert statuses == ("DELETING", "DELETING")
        assert tuple(event.phase for event in ledger.chain(deletion_id)) == (
            "PREPARED",
            "REQUESTED",
        )
        with sessions() as database:
            intent_count = database.scalar(
                select(func.count())
                .select_from(DeletionIntentRecord)
                .where(DeletionIntentRecord.owner_user_id == owner_id)
            )
            control = database.get(DeletionControlOperationRecord, deletion_id)
            user = database.get(UserRecord, owner_id)
            deletion_job_count = database.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(JobRecord.owner_user_id == owner_id, JobRecord.kind == "DELETION")
            )
            assert intent_count == 1 and deletion_job_count == 1
            assert control is not None and control.deletion_job_id is not None
            assert user is not None
            assert (user.lifecycle_state, user.lifecycle_generation) == ("DELETING", 1)
        worker = DeletionWorker(
            worker_id="postgres-deletion-worker",
            jobs=JobService(sessions),
            sweeps=DeletionSweepService(
                sessions,
                FilesystemObjectStore(tmp_path / "objects"),
                ledger,
            ),
        )
        assert worker.run_once(now=now)
        assert tuple(event.phase for event in ledger.chain(deletion_id)) == (
            "PREPARED",
            "REQUESTED",
            "COMPLETED",
        )
        assert (
            coordinator.status(
                deletion_id=deletion_id,
                receipt_secret=secret,
                now=now,
            ).status
            == "DELETED"
        )
        with sessions() as database:
            assert database.get(UserRecord, owner_id) is None
            assert database.get(DeletionAuditRecord, deletion_id) is not None
    finally:
        with sessions.begin() as database:
            database.execute(
                delete(DeletionControlOperationRecord).where(
                    DeletionControlOperationRecord.deletion_id == deletion_id
                )
            )
            job_ids = database.scalars(
                select(JobRecord.id).where(JobRecord.owner_user_id == owner_id)
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
                delete(DeletionIntentRecord).where(DeletionIntentRecord.owner_user_id == owner_id)
            )
            database.execute(
                delete(DeletionReceiptRecord).where(
                    DeletionReceiptRecord.deletion_id == deletion_id
                )
            )
            database.execute(
                delete(DeletionAuditRecord).where(DeletionAuditRecord.deletion_id == deletion_id)
            )
            database.execute(delete(SessionRecord).where(SessionRecord.user_id == owner_id))
            database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
        engine.dispose()
