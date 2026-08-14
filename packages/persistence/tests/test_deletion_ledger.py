from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from ratereplay_persistence import deletion_ledger as ledger_module
from ratereplay_persistence.deletion_ledger import (
    AAD_DOMAIN,
    DeletionLedgerError,
    FilesystemDeletionLedger,
    LedgerEvent,
    LedgerPhase,
)
from ratereplay_persistence.keyrings import VersionedKeyring

NOW = datetime(2026, 8, 14, tzinfo=UTC)
DELETION_ID = "1" * 32
SCOPE_TOKEN = "2" * 64


def _append(
    ledger: FilesystemDeletionLedger,
    phase: LedgerPhase,
    *,
    now: datetime = NOW,
) -> LedgerEvent:
    return ledger.append(
        deletion_id=DELETION_ID,
        phase=phase,
        scope_token=SCOPE_TOKEN,
        restore_key_version="restore-key-v1",
        original_generation=3,
        proposed_generation=4,
        preparation_digest="5" * 64,
        intent_proof_digest="6" * 64,
        occurred_at=now,
    )


def _stream(root: Path) -> Path:
    return root / "deletion-ledger-v2.jsonl"


def _records(root: Path) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], json.loads(line))
        for line in _stream(root).read_text(encoding="ascii").splitlines()
    ]


def _write_records(root: Path, records: list[dict[str, Any]]) -> None:
    _stream(root).write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records
        ),
        encoding="ascii",
    )


def _rehash(record: dict[str, Any]) -> None:
    unsigned = dict(record)
    unsigned.pop("record_sha256")
    record["record_sha256"] = hashlib.sha256(ledger_module._canonical(unsigned)).hexdigest()


def _decrypt_payload(ledger: FilesystemDeletionLedger, record: dict[str, Any]) -> dict[str, Any]:
    header = {key: record[key] for key in ledger_module.HEADER_KEYS}
    plaintext = AESGCM(ledger._derived_key(record["key_version"], "envelope-encryption")).decrypt(
        bytes.fromhex(record["nonce"]),
        base64.b64decode(record["ciphertext"]),
        AAD_DOMAIN + ledger_module._canonical(header),
    )
    return cast(dict[str, Any], json.loads(plaintext))


def test_ledger_encrypts_events_and_access_audits_complete_chain(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    ledger = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
    prepared = _append(ledger, "PREPARED")
    requested = _append(ledger, "REQUESTED", now=NOW + timedelta(seconds=1))
    completed = _append(ledger, "COMPLETED", now=NOW + timedelta(seconds=2))

    assert requested.previous_receipt == prepared.receipt
    assert completed.previous_receipt == requested.receipt
    assert tuple(event.phase for event in ledger.chain(DELETION_ID)) == (
        "PREPARED",
        "REQUESTED",
        "COMPLETED",
    )
    assert ledger.unresolved_preparations() == ()
    FilesystemDeletionLedger(root, integrity_key=b"k" * 32).validate()

    raw = b"".join(path.read_bytes() for path in root.iterdir() if path.is_file())
    for sensitive in (
        DELETION_ID.encode(),
        SCOPE_TOKEN.encode(),
        ("5" * 64).encode(),
        ("6" * 64).encode(),
    ):
        assert sensitive not in raw
    record_types = [record["record_type"] for record in _records(root)]
    assert "ACCESS_AUDIT" in record_types
    assert "LEDGER_EVENT" in record_types


def test_access_audit_is_encrypted_allowlisted_and_precedes_event(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    ledger = FilesystemDeletionLedger(root, integrity_key=b"k" * 32, actor="API_COORDINATOR")
    _append(ledger, "PREPARED")
    records = _records(root)

    assert [record["record_type"] for record in records] == ["ACCESS_AUDIT", "LEDGER_EVENT"]
    audit = _decrypt_payload(ledger, records[0])
    assert set(audit) == {
        "schema_version",
        "operation_id",
        "actor",
        "operation",
        "occurred_at",
        "previous_sequence",
        "previous_record_sha256",
        "outcome",
    }
    assert audit["actor"] == "API_COORDINATOR"
    assert audit["operation"] == "APPEND_PREPARED"
    assert audit["outcome"] == "AUTHORIZED"
    assert DELETION_ID not in json.dumps(audit)


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
            deletion_id=DELETION_ID,
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


@pytest.mark.parametrize(
    ("field", "replacement", "expected_code"),
    [
        ("sequence", 99, "LEDGER_SEQUENCE_INVALID"),
        ("previous_record_sha256", "f" * 64, "LEDGER_GLOBAL_CHAIN_BROKEN"),
        ("nonce", "00" * 12, "LEDGER_RECORD_AUTHENTICATION_FAILED"),
        ("record_type", "LEDGER_EVENT", "LEDGER_RECORD_AUTHENTICATION_FAILED"),
    ],
)
def test_tampered_envelope_fields_fail_closed(
    tmp_path: Path,
    field: str,
    replacement: object,
    expected_code: str,
) -> None:
    root = tmp_path / "ledger"
    ledger = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
    _append(ledger, "PREPARED")
    records = _records(root)
    records[0][field] = replacement
    _rehash(records[0])
    _write_records(root, records)

    with pytest.raises(DeletionLedgerError) as tampered:
        ledger.validate()
    assert tampered.value.code == expected_code


def test_tampered_ciphertext_tag_record_hash_genesis_and_head_fail_closed(tmp_path: Path) -> None:
    mutations = ("ciphertext", "record_hash", "genesis", "head")
    for mutation in mutations:
        root = tmp_path / mutation
        ledger = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
        _append(ledger, "PREPARED")
        if mutation in {"ciphertext", "record_hash"}:
            records = _records(root)
            if mutation == "ciphertext":
                ciphertext = bytearray(base64.b64decode(records[0]["ciphertext"]))
                ciphertext[-1] ^= 1
                records[0]["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
                _rehash(records[0])
            else:
                records[0]["record_sha256"] = "0" * 64
            _write_records(root, records)
        else:
            path = root / f"deletion-ledger-{mutation}-v2.json"
            payload = json.loads(path.read_text(encoding="ascii"))
            payload["ledger_id"] = "0" * 32
            path.write_text(json.dumps(payload), encoding="ascii")
        with pytest.raises(DeletionLedgerError):
            ledger.validate()


def test_gap_reorder_middle_removal_and_truncation_fail_closed(tmp_path: Path) -> None:
    for mutation in ("gap", "reorder", "middle", "truncate"):
        root = tmp_path / mutation
        ledger = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
        _append(ledger, "PREPARED")
        _append(ledger, "REQUESTED", now=NOW + timedelta(seconds=1))
        records = _records(root)
        if mutation == "gap":
            records[1]["sequence"] = 50
            _rehash(records[1])
        elif mutation == "reorder":
            records[1], records[2] = records[2], records[1]
        elif mutation == "middle":
            records.pop(1)
        else:
            records.pop()
        _write_records(root, records)
        with pytest.raises(DeletionLedgerError):
            ledger.validate()


def test_audit_tamper_and_audit_write_failure_prevent_all_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tampered"
    tampered = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
    _append(tampered, "PREPARED")
    records = _records(root)
    ciphertext = bytearray(base64.b64decode(records[0]["ciphertext"]))
    ciphertext[0] ^= 1
    records[0]["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    _rehash(records[0])
    _write_records(root, records)
    with pytest.raises(DeletionLedgerError) as audit_tamper:
        tampered.events()
    assert audit_tamper.value.code == "LEDGER_RECORD_AUTHENTICATION_FAILED"

    failure_root = tmp_path / "failure"
    failure = FilesystemDeletionLedger(failure_root, integrity_key=b"k" * 32)

    def fail_audit(_state: object, record_type: str, _payload: object) -> None:
        if record_type == "ACCESS_AUDIT":
            raise DeletionLedgerError("LEDGER_APPEND_FAILED", "seeded audit write failure")
        raise AssertionError("Event mutation ran before its access audit")

    monkeypatch.setattr(failure, "_append_record", fail_audit)
    with pytest.raises(DeletionLedgerError) as blocked:
        _append(failure, "PREPARED")
    assert blocked.value.code == "LEDGER_ACCESS_AUDIT_FAILED"
    assert _records(failure_root) == []


def test_nonce_collision_is_detected_and_access_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ledger"
    ledger = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
    _append(ledger, "PREPARED")
    reused = bytes.fromhex(_records(root)[0]["nonce"])
    monkeypatch.setattr(
        "ratereplay_persistence.deletion_ledger.secrets.token_bytes",
        lambda _size: reused,
    )

    with pytest.raises(DeletionLedgerError) as collision:
        ledger.events()
    assert collision.value.code == "LEDGER_ACCESS_AUDIT_FAILED"
    assert isinstance(collision.value.__cause__, DeletionLedgerError)
    assert collision.value.__cause__.code == "LEDGER_NONCE_REUSE"


def test_authenticated_tail_is_recovered_after_head_update_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ledger"
    ledger = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
    original_write_head = ledger._write_head
    monkeypatch.setattr(
        ledger,
        "_write_head",
        lambda _state: (_ for _ in ()).throw(OSError("seeded head failure")),
    )
    with pytest.raises(DeletionLedgerError) as failed_audit:
        ledger.events()
    assert failed_audit.value.code == "LEDGER_ACCESS_AUDIT_FAILED"
    assert len(_records(root)) == 1

    monkeypatch.setattr(ledger, "_write_head", original_write_head)
    ledger.validate()
    head = json.loads((root / "deletion-ledger-head-v2.json").read_text(encoding="ascii"))
    assert head["last_sequence"] == 2


def test_missing_historical_key_and_old_writer_configuration_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    old = VersionedKeyring.single("ledger-old", b"o" * 32)
    ledger = FilesystemDeletionLedger(root, keyring=old)
    _append(ledger, "PREPARED")

    with pytest.raises(DeletionLedgerError) as missing:
        FilesystemDeletionLedger(
            root,
            keyring=VersionedKeyring.single("ledger-new", b"n" * 32),
            require_existing=True,
        ).validate()
    assert missing.value.code == "LEDGER_KEY_VERSION_UNAVAILABLE"

    mixed = VersionedKeyring(
        current_version="ledger-new",
        keys={"ledger-old": b"o" * 32, "ledger-new": b"n" * 32},
    )
    with pytest.raises(DeletionLedgerError) as old_writer:
        FilesystemDeletionLedger(root, keyring=mixed, require_existing=True).validate()
    assert old_writer.value.code == "LEDGER_KEY_CONFIGURATION_MISMATCH"


def test_restore_mode_requires_preexisting_encrypted_ledger(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    with pytest.raises(DeletionLedgerError) as missing:
        FilesystemDeletionLedger(root, integrity_key=b"k" * 32, require_existing=True)
    assert missing.value.code == "LEDGER_MISSING"
    assert not root.exists()

    initialized = FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
    initialized.validate()
    reopened = FilesystemDeletionLedger(root, integrity_key=b"k" * 32, require_existing=True)
    reopened.validate()
    with pytest.raises(DeletionLedgerError) as wrong_key:
        FilesystemDeletionLedger(root, integrity_key=b"x" * 32, require_existing=True).validate()
    assert wrong_key.value.code == "LEDGER_SIGNATURE_INVALID"


def test_plaintext_v1_requires_explicit_offline_migration(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "deletion-ledger-v1.jsonl").write_text("", encoding="ascii")
    (root / "deletion-ledger-genesis-v1.json").write_text("{}", encoding="ascii")

    with pytest.raises(DeletionLedgerError) as migration:
        FilesystemDeletionLedger(root, integrity_key=b"k" * 32)
    assert migration.value.code == "LEDGER_FORMAT_MIGRATION_REQUIRED"
