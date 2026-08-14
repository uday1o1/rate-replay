from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import DeletionLedgerError, FilesystemDeletionLedger
from ratereplay_persistence.deletions import DeletionCoordinator, DeletionServiceError
from ratereplay_persistence.keyrings import VersionedKeyring
from ratereplay_persistence.models import (
    DeletionControlOperationRecord,
    DeletionIntentRecord,
    DeletionReceiptRecord,
    JobRecord,
    SessionRecord,
    UserRecord,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
OWNER_ID = "1" * 32
SECRET = b"s" * 32
OTHER_SECRET = b"o" * 32


@dataclass(frozen=True, slots=True)
class Harness:
    sessions: sessionmaker[Session]
    ledger: FilesystemDeletionLedger
    deletions: DeletionCoordinator


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=OWNER_ID,
                username_canonical="delete-owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
        database.add(
            SessionRecord(
                id="2" * 32,
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
            JobRecord(
                id="ordinary-job",
                owner_user_id=OWNER_ID,
                kind="REPORT",
                request_schema_version="report-v1",
                request_hash="c" * 64,
                scope_mode="ACTIVE_SCOPE",
                request_json="{}",
                captured_account_generation=0,
                state="QUEUED",
                attempt_count=0,
                max_attempts=3,
                fencing_generation=0,
                not_before=NOW,
                cancel_requested=False,
                created_at=NOW,
            )
        )
    return Harness(
        sessions,
        ledger,
        DeletionCoordinator(sessions, ledger, restore_key=b"r" * 32),
    )


def _intent(harness: Harness, *, key: str = "intent-key", secret: bytes = SECRET) -> str:
    return harness.deletions.create_intent(
        owner_user_id=OWNER_ID,
        idempotency_key=key,
        receipt_secret=secret,
        now=NOW,
    ).deletion_id


def test_deletion_coordinator_rejects_ambiguous_or_invalid_restore_key_sources(
    harness: Harness,
) -> None:
    keyring = VersionedKeyring.single("restore-v1", b"r" * 32)
    with pytest.raises(ValueError, match="exactly one"):
        DeletionCoordinator(harness.sessions, harness.ledger)
    with pytest.raises(ValueError, match="exactly one"):
        DeletionCoordinator(
            harness.sessions,
            harness.ledger,
            restore_key=b"r" * 32,
            restore_keyring=keyring,
        )
    with pytest.raises(ValueError, match="configuration is invalid"):
        DeletionCoordinator(harness.sessions, harness.ledger, restore_key=b"short")


def test_intent_is_idempotent_secret_bound_and_expires_exactly(harness: Harness) -> None:
    first = harness.deletions.create_intent(
        owner_user_id=OWNER_ID,
        idempotency_key="intent-key",
        receipt_secret=SECRET,
        now=NOW,
    )
    repeated = harness.deletions.create_intent(
        owner_user_id=OWNER_ID,
        idempotency_key="intent-key",
        receipt_secret=SECRET,
        now=NOW + timedelta(minutes=1),
    )
    assert repeated.deletion_id == first.deletion_id
    assert repeated.repeated
    with pytest.raises(DeletionServiceError, match="another deletion proof") as conflict:
        harness.deletions.create_intent(
            owner_user_id=OWNER_ID,
            idempotency_key="intent-key",
            receipt_secret=OTHER_SECRET,
            now=NOW + timedelta(minutes=1),
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(DeletionServiceError) as expired:
        harness.deletions.create_intent(
            owner_user_id=OWNER_ID,
            idempotency_key="intent-key",
            receipt_secret=SECRET,
            now=NOW + timedelta(minutes=15),
        )
    assert expired.value.code == "INTENT_EXPIRED"
    replacement = harness.deletions.create_intent(
        owner_user_id=OWNER_ID,
        idempotency_key="new-key",
        receipt_secret=OTHER_SECRET,
        now=NOW + timedelta(minutes=15),
    )
    assert replacement.deletion_id != first.deletion_id


def test_account_deletion_fences_then_requests_before_acceptance(harness: Harness) -> None:
    deletion_id = _intent(harness)
    status = harness.deletions.authorize_and_start(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW + timedelta(seconds=1),
    )
    assert status.status == "DELETING"
    assert tuple(event.phase for event in harness.ledger.chain(deletion_id)) == (
        "PREPARED",
        "REQUESTED",
    )
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        session = database.get(SessionRecord, "2" * 32)
        ordinary = database.get(JobRecord, "ordinary-job")
        control = database.get(DeletionControlOperationRecord, deletion_id)
        deletion_job = database.get(JobRecord, control.deletion_job_id if control else "")
        intent = database.get(DeletionIntentRecord, deletion_id)
        receipt = database.get(DeletionReceiptRecord, deletion_id)
        assert user is not None
        assert (user.lifecycle_state, user.lifecycle_generation) == ("DELETING", 1)
        assert user.deletion_scope_id is not None
        assert session is not None and session.revoked_at is not None
        assert ordinary is not None
        assert (ordinary.state, ordinary.cancel_requested) == ("CANCELLED", True)
        assert control is not None and control.phase == "DRAIN"
        assert deletion_job is not None
        assert (deletion_job.kind, deletion_job.scope_mode, deletion_job.state) == (
            "DELETION",
            "DELETING_SCOPE",
            "QUEUED",
        )
        assert intent is not None and intent.state == "CONSUMED"
        assert receipt is not None and SECRET.hex() not in receipt.receipt_verifier


def test_wrong_receipt_cannot_prepare_or_poll(harness: Harness) -> None:
    deletion_id = _intent(harness)
    with pytest.raises(DeletionServiceError) as start_error:
        harness.deletions.authorize_and_start(
            owner_user_id=OWNER_ID,
            deletion_id=deletion_id,
            receipt_secret=OTHER_SECRET,
            now=NOW + timedelta(seconds=1),
        )
    assert start_error.value.code == "INVALID_DELETION_PROOF"
    assert harness.ledger.chain(deletion_id) == ()
    with pytest.raises(DeletionServiceError) as status_error:
        harness.deletions.status(
            deletion_id=deletion_id,
            receipt_secret=OTHER_SECRET,
            now=NOW,
        )
    assert status_error.value.code == "INVALID_DELETION_PROOF"


def test_startup_reconciler_recovers_crash_after_prepared(harness: Harness) -> None:
    deletion_id = _intent(harness)
    intent = harness.deletions._authorized_intent(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW,
    )
    harness.deletions._ensure_prepared(intent, now=NOW + timedelta(seconds=1))
    assert len(harness.ledger.unresolved_preparations()) == 1
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None and user.lifecycle_state == "ACTIVE"
    reconciled = harness.deletions.reconcile(now=NOW + timedelta(seconds=2))
    assert (
        reconciled.prepared_examined,
        reconciled.controls_examined,
        reconciled.advanced,
        reconciled.quarantined,
    ) == (1, 0, 1, 0)
    assert tuple(event.phase for event in harness.ledger.chain(deletion_id)) == (
        "PREPARED",
        "REQUESTED",
    )
    assert (
        harness.deletions.status(
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=NOW + timedelta(seconds=2),
        ).status
        == "DELETING"
    )


def test_reconciler_continues_old_preparation_after_restore_key_rotation(
    harness: Harness,
) -> None:
    deletion_id = _intent(harness)
    intent = harness.deletions._authorized_intent(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW,
    )
    harness.deletions._ensure_prepared(intent, now=NOW + timedelta(seconds=1))
    rotated = DeletionCoordinator(
        harness.sessions,
        harness.ledger,
        restore_keyring=VersionedKeyring(
            current_version="restore-v2",
            keys={"restore-v1": b"r" * 32, "restore-v2": b"n" * 32},
        ),
    )

    result = rotated.reconcile(now=NOW + timedelta(seconds=2))

    assert (result.advanced, result.quarantined) == (1, 0)
    chain = harness.ledger.chain(deletion_id)
    assert tuple(event.phase for event in chain) == ("PREPARED", "REQUESTED")
    assert {event.restore_key_version for event in chain} == {"restore-v1"}


def test_reconciler_quarantines_old_preparation_without_historical_restore_key(
    harness: Harness,
) -> None:
    deletion_id = _intent(harness)
    intent = harness.deletions._authorized_intent(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW,
    )
    harness.deletions._ensure_prepared(intent, now=NOW + timedelta(seconds=1))
    rotated = DeletionCoordinator(
        harness.sessions,
        harness.ledger,
        restore_keyring=VersionedKeyring.single("restore-v2", b"n" * 32),
    )

    result = rotated.reconcile(now=NOW + timedelta(seconds=2))

    assert (result.advanced, result.quarantined) == (0, 1)
    assert tuple(event.phase for event in harness.ledger.chain(deletion_id)) == ("PREPARED",)
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None and user.lifecycle_state == "ACTIVE"


def test_failed_requested_append_leaves_target_fenced_until_retry(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deletion_id = _intent(harness)
    real_append = harness.ledger.append
    failed = False

    def fail_requested(**arguments: object) -> object:
        nonlocal failed
        if arguments["phase"] == "REQUESTED" and not failed:
            failed = True
            raise DeletionLedgerError("TEST_REQUESTED_FAILURE", "injected failure")
        return real_append(**arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.ledger, "append", fail_requested)
    with pytest.raises(DeletionLedgerError, match="injected failure"):
        harness.deletions.authorize_and_start(
            owner_user_id=OWNER_ID,
            deletion_id=deletion_id,
            receipt_secret=SECRET,
            now=NOW + timedelta(seconds=1),
        )
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None
        assert (user.lifecycle_state, user.lifecycle_generation) == (
            "DELETION_PENDING_LEDGER",
            1,
        )
    monkeypatch.setattr(harness.ledger, "append", real_append)
    reconciled = harness.deletions.reconcile(now=NOW + timedelta(seconds=2))
    assert reconciled.advanced >= 1
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None and user.lifecycle_state == "DELETING"


def test_generation_mismatch_keeps_preparation_quarantined(harness: Harness) -> None:
    deletion_id = _intent(harness)
    intent = harness.deletions._authorized_intent(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW,
    )
    harness.deletions._ensure_prepared(intent, now=NOW + timedelta(seconds=1))
    with harness.sessions.begin() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None
        user.lifecycle_generation = 9
    result = harness.deletions.reconcile(now=NOW + timedelta(seconds=2))
    assert result.quarantined == 1
    assert len(harness.ledger.unresolved_preparations()) == 1
    with harness.sessions() as database:
        assert database.get(DeletionControlOperationRecord, deletion_id) is None


def test_strict_noncommit_proof_aborts_without_fencing(harness: Harness) -> None:
    deletion_id = _intent(harness)
    intent = harness.deletions._authorized_intent(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW,
    )
    harness.deletions._ensure_prepared(intent, now=NOW + timedelta(seconds=1))
    harness.deletions.prove_noncommit_and_abort(
        deletion_id=deletion_id,
        now=NOW + timedelta(seconds=2),
    )
    assert tuple(event.phase for event in harness.ledger.chain(deletion_id)) == (
        "PREPARED",
        "ABORTED",
    )
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        stored_intent = database.get(DeletionIntentRecord, deletion_id)
        assert user is not None and user.lifecycle_state == "ACTIVE"
        assert stored_intent is not None and stored_intent.state == "INVALIDATED"
    replacement = harness.deletions.create_intent(
        owner_user_id=OWNER_ID,
        idempotency_key="after-abort",
        receipt_secret=OTHER_SECRET,
        now=NOW + timedelta(seconds=3),
    )
    assert replacement.deletion_id != deletion_id


def test_abort_is_rejected_after_fence_commits(harness: Harness) -> None:
    deletion_id = _intent(harness)
    harness.deletions.authorize_and_start(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(DeletionServiceError) as raised:
        harness.deletions.prove_noncommit_and_abort(
            deletion_id=deletion_id,
            now=NOW + timedelta(seconds=2),
        )
    assert raised.value.code == "ABORT_NOT_PROVABLE"


def test_no_requested_event_coexists_with_active_target(harness: Harness) -> None:
    deletion_id = _intent(harness)
    harness.deletions.authorize_and_start(
        owner_user_id=OWNER_ID,
        deletion_id=deletion_id,
        receipt_secret=SECRET,
        now=NOW + timedelta(seconds=1),
    )
    requested = harness.ledger.chain(deletion_id)[-1]
    assert requested.phase == "REQUESTED"
    with harness.sessions() as database:
        control = database.get(DeletionControlOperationRecord, deletion_id)
        assert control is not None
        user = database.scalar(
            select(UserRecord).where(UserRecord.deletion_scope_id == control.target_scope_id)
        )
        assert user is not None and user.lifecycle_state != "ACTIVE"
