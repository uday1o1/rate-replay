from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ratereplay_persistence.deletion_ledger import (
    DeletionLedgerError,
    FilesystemDeletionLedger,
)

NOW = datetime(2026, 8, 14, tzinfo=UTC)


def _append(ledger: FilesystemDeletionLedger, phase: str, *, now: datetime = NOW):  # type: ignore[no-untyped-def]
    return ledger.append(
        deletion_id="1" * 32,
        phase=phase,  # type: ignore[arg-type]
        scope_token="2" * 64,
        restore_key_version="restore-key-v1",
        original_generation=3,
        proposed_generation=4,
        preparation_digest="5" * 64,
        intent_proof_digest="6" * 64,
        occurred_at=now,
    )


def test_ledger_persists_and_validates_complete_chain(tmp_path: Path) -> None:
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"k" * 32)
    prepared = _append(ledger, "PREPARED")
    requested = _append(ledger, "REQUESTED", now=NOW + timedelta(seconds=1))
    completed = _append(ledger, "COMPLETED", now=NOW + timedelta(seconds=2))
    assert requested.previous_receipt == prepared.receipt
    assert completed.previous_receipt == requested.receipt
    assert tuple(event.phase for event in ledger.chain("1" * 32)) == (
        "PREPARED",
        "REQUESTED",
        "COMPLETED",
    )
    assert ledger.unresolved_preparations() == ()
    FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"k" * 32).validate()


def test_unresolved_preparation_is_durable_and_enumerated(tmp_path: Path) -> None:
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"k" * 32)
    prepared = _append(ledger, "PREPARED")
    reopened = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"k" * 32)
    assert reopened.unresolved_preparations() == (prepared,)


def test_only_two_legal_chains_are_admitted(tmp_path: Path) -> None:
    completed = FilesystemDeletionLedger(tmp_path / "complete", integrity_key=b"k" * 32)
    _append(completed, "PREPARED")
    with pytest.raises(DeletionLedgerError) as skipped_request:
        _append(completed, "COMPLETED")
    assert skipped_request.value.code == "ILLEGAL_LEDGER_CHAIN"

    aborted = FilesystemDeletionLedger(tmp_path / "aborted", integrity_key=b"k" * 32)
    _append(aborted, "PREPARED")
    _append(aborted, "ABORTED", now=NOW + timedelta(seconds=1))
    with pytest.raises(DeletionLedgerError) as request_after_abort:
        _append(aborted, "REQUESTED", now=NOW + timedelta(seconds=2))
    assert request_after_abort.value.code == "ILLEGAL_LEDGER_CHAIN"


def test_duplicate_or_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"k" * 32)
    prepared = _append(ledger, "PREPARED")
    assert _append(ledger, "PREPARED") == prepared
    with pytest.raises(DeletionLedgerError) as duplicate:
        _append(ledger, "PREPARED", now=NOW + timedelta(seconds=1))
    assert duplicate.value.code == "LEDGER_DUPLICATE_MISMATCH"
    with pytest.raises(DeletionLedgerError) as identity:
        ledger.append(
            deletion_id="1" * 32,
            phase="REQUESTED",
            scope_token="9" * 64,
            restore_key_version="restore-key-v1",
            original_generation=3,
            proposed_generation=4,
            preparation_digest="5" * 64,
            intent_proof_digest="6" * 64,
            occurred_at=NOW + timedelta(seconds=2),
        )
    assert identity.value.code == "LEDGER_IDENTITY_MISMATCH"


def test_tampering_or_wrong_key_fails_closed(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    ledger = FilesystemDeletionLedger(ledger_root, integrity_key=b"k" * 32)
    event = _append(ledger, "PREPARED")
    ledger_path = ledger_root / "deletion-ledger-v1.jsonl"
    payload = asdict(event)
    payload["proposed_generation"] = 99
    ledger_path.write_text(json.dumps(payload) + "\n", encoding="ascii")
    with pytest.raises(DeletionLedgerError) as tampered:
        ledger.validate()
    assert tampered.value.code == "LEDGER_RECEIPT_INVALID"
    with pytest.raises(DeletionLedgerError) as wrong_key:
        FilesystemDeletionLedger(ledger_root, integrity_key=b"x" * 32).validate()
    assert wrong_key.value.code == "LEDGER_RECEIPT_INVALID"


def test_restore_mode_requires_preexisting_keyed_ledger(tmp_path: Path) -> None:
    ledger_root = tmp_path / "ledger"
    with pytest.raises(DeletionLedgerError) as missing:
        FilesystemDeletionLedger(
            ledger_root,
            integrity_key=b"k" * 32,
            require_existing=True,
        )
    assert missing.value.code == "LEDGER_MISSING"
    assert not ledger_root.exists()

    initialized = FilesystemDeletionLedger(ledger_root, integrity_key=b"k" * 32)
    initialized.validate()
    reopened = FilesystemDeletionLedger(
        ledger_root,
        integrity_key=b"k" * 32,
        require_existing=True,
    )
    reopened.validate()
    with pytest.raises(DeletionLedgerError) as wrong_empty_key:
        FilesystemDeletionLedger(
            ledger_root,
            integrity_key=b"x" * 32,
            require_existing=True,
        ).validate()
    assert wrong_empty_key.value.code == "LEDGER_RECEIPT_INVALID"
