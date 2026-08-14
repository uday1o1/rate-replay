from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ratereplay_persistence.deletion_ledger import (
    DeletionLedgerError,
    FilesystemDeletionLedger,
    LedgerEvent,
    LedgerPhase,
)
from ratereplay_persistence.deletion_ledger_migration import (
    MIGRATION_MARKER,
    migrate_plaintext_v1_ledger,
    verify_migration_artifact,
    write_migration_artifact,
)
from ratereplay_persistence.keyrings import VersionedKeyring

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
LEGACY_KEY = b"legacy-integrity-key-material-v1!!"
DELETION_ID = "1" * 32
SCOPE_TOKEN = "2" * 64


def _legacy_source(
    root: Path,
    *,
    phases: tuple[LedgerPhase, ...] = ("PREPARED",),
) -> tuple[LedgerEvent, ...]:
    root.mkdir()
    genesis_receipt = hmac.new(
        LEGACY_KEY,
        b"RateReplay.DeletionLedgerGenesis.v1\x00",
        hashlib.sha256,
    ).hexdigest()
    (root / "deletion-ledger-genesis-v1.json").write_text(
        json.dumps(
            {
                "schema_version": "deletion-ledger-genesis-v1",
                "receipt": genesis_receipt,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )
    events: list[LedgerEvent] = []
    for offset, phase in enumerate(phases):
        unsigned = LedgerEvent(
            schema_version="deletion-ledger-event-v1",
            deletion_id=DELETION_ID,
            phase=phase,
            scope_token=SCOPE_TOKEN,
            restore_key_version="restore-v1",
            original_generation=3,
            proposed_generation=4,
            preparation_digest="5" * 64,
            intent_proof_digest="6" * 64,
            occurred_at=(NOW + timedelta(seconds=offset)).isoformat(),
            previous_receipt=events[-1].receipt if events else None,
            receipt="",
        )
        receipt = hmac.new(
            LEGACY_KEY,
            b"RateReplay.DeletionLedgerReceipt.v1\x00" + unsigned.canonical_without_receipt(),
            hashlib.sha256,
        ).hexdigest()
        events.append(LedgerEvent(**{**asdict(unsigned), "receipt": receipt}))
    (root / "deletion-ledger-v1.jsonl").write_text(
        "".join(
            json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n"
            for event in events
        ),
        encoding="ascii",
    )
    return tuple(events)


def _ledger_keyring() -> VersionedKeyring:
    return VersionedKeyring.single("ledger-v2", b"n" * 32)


def _restore_keyring() -> VersionedKeyring:
    return VersionedKeyring(
        current_version="restore-v2",
        keys={"restore-v1": b"r" * 32, "restore-v2": b"s" * 32},
    )


def test_migration_preserves_exact_events_receipts_and_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    events = _legacy_source(source)
    source_before = {path.name: path.read_bytes() for path in source.iterdir()}

    artifact = migrate_plaintext_v1_ledger(
        source,
        destination,
        legacy_integrity_key=LEGACY_KEY,
        ledger_keyring=_ledger_keyring(),
        restore_keyring=_restore_keyring(),
        migrated_at=NOW,
    )

    assert verify_migration_artifact(asdict(artifact)) == artifact
    assert artifact.source_event_count == 1
    assert {path.name: path.read_bytes() for path in source.iterdir()} == source_before
    assert not (destination / MIGRATION_MARKER).exists()
    ledger = FilesystemDeletionLedger(
        destination,
        keyring=_ledger_keyring(),
        restore_key_version="restore-v2",
        require_existing=True,
    )
    assert ledger.events() == events
    duplicate = ledger.append(
        deletion_id=events[0].deletion_id,
        phase="PREPARED",
        scope_token=events[0].scope_token,
        restore_key_version=events[0].restore_key_version,
        original_generation=events[0].original_generation,
        proposed_generation=events[0].proposed_generation,
        preparation_digest=events[0].preparation_digest,
        intent_proof_digest=events[0].intent_proof_digest,
        occurred_at=datetime.fromisoformat(events[0].occurred_at),
    )
    assert duplicate == events[0]
    requested = ledger.append(
        deletion_id=events[0].deletion_id,
        phase="REQUESTED",
        scope_token=events[0].scope_token,
        restore_key_version=events[0].restore_key_version,
        original_generation=events[0].original_generation,
        proposed_generation=events[0].proposed_generation,
        preparation_digest=events[0].preparation_digest,
        intent_proof_digest=events[0].intent_proof_digest,
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert requested.schema_version == "deletion-ledger-event-v2"
    assert requested.previous_receipt == events[0].receipt
    raw_destination = b"".join(
        path.read_bytes() for path in destination.iterdir() if path.is_file()
    )
    assert DELETION_ID.encode() not in raw_destination
    assert SCOPE_TOKEN.encode() not in raw_destination
    artifact_path = tmp_path / "migration.json"
    write_migration_artifact(artifact_path, artifact)
    assert artifact_path.stat().st_mode & 0o777 == 0o600
    tampered = asdict(artifact)
    tampered["source_event_count"] = 99
    with pytest.raises(DeletionLedgerError) as invalid_artifact:
        verify_migration_artifact(tampered)
    assert invalid_artifact.value.code == "LEDGER_MIGRATION_ARTIFACT_INVALID"


def test_bad_legacy_receipt_fails_and_leaves_incomplete_destination_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _legacy_source(source)
    stream = source / "deletion-ledger-v1.jsonl"
    payload = json.loads(stream.read_text(encoding="ascii"))
    payload["receipt"] = "0" * 64
    stream.write_text(json.dumps(payload) + "\n", encoding="ascii")

    with pytest.raises(DeletionLedgerError) as invalid:
        migrate_plaintext_v1_ledger(
            source,
            destination,
            legacy_integrity_key=LEGACY_KEY,
            ledger_keyring=_ledger_keyring(),
            restore_keyring=_restore_keyring(),
            migrated_at=NOW,
        )

    assert invalid.value.code == "LEGACY_LEDGER_RECEIPT_INVALID"
    assert (destination / MIGRATION_MARKER).is_file()
    with pytest.raises(DeletionLedgerError) as incomplete:
        FilesystemDeletionLedger(destination, integrity_key=b"x" * 32)
    assert incomplete.value.code == "LEDGER_MIGRATION_INCOMPLETE"


def test_migration_rejects_nonempty_destination_and_missing_restore_key(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _legacy_source(source)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "operator-owned"
    marker.write_text("keep", encoding="ascii")

    with pytest.raises(DeletionLedgerError) as nonempty:
        migrate_plaintext_v1_ledger(
            source,
            occupied,
            legacy_integrity_key=LEGACY_KEY,
            ledger_keyring=_ledger_keyring(),
            restore_keyring=_restore_keyring(),
            migrated_at=NOW,
        )
    assert nonempty.value.code == "LEDGER_MIGRATION_DESTINATION_NOT_EMPTY"
    assert marker.read_text(encoding="ascii") == "keep"

    missing_destination = tmp_path / "missing-key"
    with pytest.raises(DeletionLedgerError) as missing:
        migrate_plaintext_v1_ledger(
            source,
            missing_destination,
            legacy_integrity_key=LEGACY_KEY,
            ledger_keyring=_ledger_keyring(),
            restore_keyring=VersionedKeyring.single("restore-v2", b"s" * 32),
            migrated_at=NOW,
        )
    assert missing.value.code == "RESTORE_KEY_VERSION_UNAVAILABLE"
    assert (source / "deletion-ledger-v1.jsonl").is_file()


def test_migration_rejects_same_source_and_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _legacy_source(source)

    with pytest.raises(DeletionLedgerError) as same_path:
        migrate_plaintext_v1_ledger(
            source,
            source,
            legacy_integrity_key=LEGACY_KEY,
            ledger_keyring=_ledger_keyring(),
            restore_keyring=_restore_keyring(),
            migrated_at=NOW,
        )

    assert same_path.value.code == "LEDGER_MIGRATION_PATH_INVALID"


def test_migration_rejects_invalid_time_key_genesis_and_phase_chain(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _legacy_source(source)
    with pytest.raises(TypeError, match="timezone-aware"):
        migrate_plaintext_v1_ledger(
            source,
            tmp_path / "naive",
            legacy_integrity_key=LEGACY_KEY,
            ledger_keyring=_ledger_keyring(),
            restore_keyring=_restore_keyring(),
            migrated_at=datetime(2026, 8, 14, 12),
        )
    with pytest.raises(ValueError, match="at least 32"):
        migrate_plaintext_v1_ledger(
            source,
            tmp_path / "short-key",
            legacy_integrity_key=b"short",
            ledger_keyring=_ledger_keyring(),
            restore_keyring=_restore_keyring(),
            migrated_at=NOW,
        )

    bad_genesis = tmp_path / "bad-genesis"
    _legacy_source(bad_genesis)
    genesis_path = bad_genesis / "deletion-ledger-genesis-v1.json"
    genesis = json.loads(genesis_path.read_text(encoding="ascii"))
    genesis["receipt"] = "0" * 64
    genesis_path.write_text(json.dumps(genesis) + "\n", encoding="ascii")
    with pytest.raises(DeletionLedgerError) as invalid_genesis:
        migrate_plaintext_v1_ledger(
            bad_genesis,
            tmp_path / "bad-genesis-destination",
            legacy_integrity_key=LEGACY_KEY,
            ledger_keyring=_ledger_keyring(),
            restore_keyring=_restore_keyring(),
            migrated_at=NOW,
        )
    assert invalid_genesis.value.code == "LEGACY_LEDGER_RECEIPT_INVALID"

    illegal = tmp_path / "illegal"
    _legacy_source(illegal, phases=("PREPARED", "COMPLETED"))
    with pytest.raises(DeletionLedgerError) as invalid_chain:
        migrate_plaintext_v1_ledger(
            illegal,
            tmp_path / "illegal-destination",
            legacy_integrity_key=LEGACY_KEY,
            ledger_keyring=_ledger_keyring(),
            restore_keyring=_restore_keyring(),
            migrated_at=NOW,
        )
    assert invalid_chain.value.code == "LEGACY_LEDGER_CHAIN_INVALID"


def test_legacy_import_is_restricted_to_verified_v1_events(tmp_path: Path) -> None:
    source = tmp_path / "source"
    events = _legacy_source(source)
    ordinary = FilesystemDeletionLedger(tmp_path / "ordinary", integrity_key=b"k" * 32)
    with pytest.raises(DeletionLedgerError) as actor:
        ordinary.import_legacy_events(events, source_sha256="0" * 64, migrated_at=NOW)
    assert actor.value.code == "LEDGER_MIGRATION_ACTOR_REQUIRED"

    migration = FilesystemDeletionLedger(
        tmp_path / "migration",
        integrity_key=b"k" * 32,
        actor="MIGRATION_CLI",
    )
    native = LedgerEvent(**{**asdict(events[0]), "schema_version": "deletion-ledger-event-v2"})
    with pytest.raises(DeletionLedgerError) as event:
        migration.import_legacy_events(
            (native,),
            source_sha256="0" * 64,
            migrated_at=NOW,
        )
    assert event.value.code == "LEDGER_MIGRATION_EVENT_INVALID"
