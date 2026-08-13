from datetime import UTC, datetime, timedelta

import pytest
from ratereplay_domain.deletion_protocol import (
    DeletionIntentRegistry,
    DeletionProtocolError,
    DeletionProtocolState,
    LedgerPhase,
    LifecycleState,
)


def test_full_legal_deletion_chain_and_atomic_terminal_transition() -> None:
    state = DeletionProtocolState().append_prepared()
    assert not state.restore_exposure_allowed
    state = state.fence_database()
    assert state.lifecycle is LifecycleState.DELETION_PENDING_LEDGER
    state = state.append_requested()
    state.validate()
    state = state.begin_sweep().checkpoint("SWEEP").checkpoint("VERIFY")
    state = state.append_completed()
    state.validate()
    state = state.finalize_deleted()
    assert state.lifecycle is LifecycleState.DELETED
    assert state.ledger_chain == (
        LedgerPhase.PREPARED,
        LedgerPhase.REQUESTED,
        LedgerPhase.COMPLETED,
    )


def test_requested_cannot_precede_database_fence() -> None:
    with pytest.raises(DeletionProtocolError) as raised:
        DeletionProtocolState().append_prepared().append_requested()
    assert raised.value.code == "REQUEST_REQUIRES_FENCE"


def test_fence_and_sweep_cannot_skip_protocol_phases() -> None:
    with pytest.raises(DeletionProtocolError) as fence:
        DeletionProtocolState().fence_database()
    assert fence.value.code == "FENCE_REQUIRES_PREPARED"
    with pytest.raises(DeletionProtocolError) as sweep:
        DeletionProtocolState().append_prepared().fence_database().begin_sweep()
    assert sweep.value.code == "SWEEP_REQUIRES_REQUESTED"


def test_unresolved_preparation_blocks_restore_exposure() -> None:
    assert not DeletionProtocolState().append_prepared().restore_exposure_allowed


@pytest.mark.parametrize(
    "ledger_chain",
    [
        (LedgerPhase.PREPARED, LedgerPhase.REQUESTED),
        (LedgerPhase.PREPARED, LedgerPhase.REQUESTED, LedgerPhase.COMPLETED),
    ],
)
def test_suppressive_ledger_event_cannot_coexist_with_active_target(
    ledger_chain: tuple[LedgerPhase, ...],
) -> None:
    state = DeletionProtocolState(
        lifecycle=LifecycleState.ACTIVE,
        ledger_chain=ledger_chain,
    )
    with pytest.raises(DeletionProtocolError) as raised:
        state.validate()
    assert raised.value.code == "SUPPRESSIVE_EVENT_WITH_ACTIVE_TARGET"


def test_abort_requires_positive_noncommit_evidence() -> None:
    prepared = DeletionProtocolState().append_prepared()
    with pytest.raises(DeletionProtocolError):
        prepared.append_aborted(authoritative_noncommit=False)
    aborted = prepared.append_aborted(authoritative_noncommit=True)
    assert aborted.restore_exposure_allowed


def test_sweep_crash_preserves_resumable_control_state() -> None:
    sweeping = (
        DeletionProtocolState()
        .append_prepared()
        .fence_database()
        .append_requested()
        .begin_sweep()
        .checkpoint("SWEEP")
    )
    resumed = sweeping.checkpoint("VERIFY").append_completed().finalize_deleted()
    assert resumed.lifecycle is LifecycleState.DELETED
    assert sweeping.ledger_chain == (LedgerPhase.PREPARED, LedgerPhase.REQUESTED)
    assert sweeping.intent_consumed
    assert sweeping.generation == 1
    assert sweeping.sweep_checkpoint == "SWEEP"


def test_terminal_append_crash_preserves_resumable_control_state() -> None:
    completed = (
        DeletionProtocolState()
        .append_prepared()
        .fence_database()
        .append_requested()
        .begin_sweep()
        .checkpoint("VERIFY")
        .append_completed()
    )
    assert completed.lifecycle is LifecycleState.DELETING
    assert completed.ledger_chain[-1] is LedgerPhase.COMPLETED
    assert completed.finalize_deleted().lifecycle is LifecycleState.DELETED


def test_intent_idempotency_expiry_consumption_and_status_after_revocation() -> None:
    registry = DeletionIntentRegistry()
    now = datetime(2026, 8, 13, tzinfo=UTC)
    secret = bytes(range(32))
    intent = registry.create(
        owner_id="owner-1", idempotency_key="key-1", receipt_secret=secret, now=now
    )
    repeated = registry.create(
        owner_id="owner-1", idempotency_key="key-1", receipt_secret=secret, now=now
    )
    assert repeated.deletion_id == intent.deletion_id
    consumed = registry.consume(
        deletion_id=intent.deletion_id,
        owner_id="owner-1",
        receipt_secret=secret,
        now=now + timedelta(minutes=1),
    )
    assert consumed.session_revoked
    assert registry.status(deletion_id=intent.deletion_id, receipt_secret=secret) == (
        "DELETION_PENDING_LEDGER"
    )


def test_expired_intent_is_not_resurrected() -> None:
    registry = DeletionIntentRegistry()
    now = datetime(2026, 8, 13, tzinfo=UTC)
    secret = b"x" * 32
    registry.create(owner_id="owner", idempotency_key="key", receipt_secret=secret, now=now)
    with pytest.raises(DeletionProtocolError) as raised:
        registry.create(
            owner_id="owner",
            idempotency_key="key",
            receipt_secret=secret,
            now=now + timedelta(minutes=15),
        )
    assert raised.value.code == "INTENT_EXPIRED"


def test_lost_response_recovery_does_not_need_normal_session() -> None:
    registry = DeletionIntentRegistry()
    now = datetime(2026, 8, 13, tzinfo=UTC)
    secret = b"z" * 32
    intent = registry.create(
        owner_id="owner", idempotency_key="key", receipt_secret=secret, now=now
    )
    registry.consume(
        deletion_id=intent.deletion_id,
        owner_id="owner",
        receipt_secret=secret,
        now=now,
    )
    assert registry.status(deletion_id=intent.deletion_id, receipt_secret=secret) == (
        "DELETION_PENDING_LEDGER"
    )
    with pytest.raises(DeletionProtocolError):
        registry.status(deletion_id=intent.deletion_id, receipt_secret=b"wrong")
