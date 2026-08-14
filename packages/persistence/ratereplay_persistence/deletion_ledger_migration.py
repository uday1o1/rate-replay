"""Offline verified migration from the plaintext v1 deletion ledger to encrypted v2."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, cast

from ratereplay_persistence.deletion_ledger import (
    LEGAL_CHAINS,
    DeletionLedgerError,
    FilesystemDeletionLedger,
    LedgerEvent,
    _atomic_write,
    _canonical,
    _file_sha256,
    _fsync_directory,
    _required_integer,
    _required_text,
    _validate_event,
)
from ratereplay_persistence.keyrings import KeyringError, VersionedKeyring

MIGRATION_MARKER = ".deletion-ledger-migration-in-progress"


@dataclass(frozen=True, slots=True)
class LedgerMigrationArtifact:
    schema_version: Literal["deletion-ledger-migration-artifact-v1"]
    source_sha256: str
    source_event_count: int
    destination_ledger_id: str
    destination_head_sha256: str
    ledger_key_version: str
    restore_key_version: str
    migrated_at: str
    artifact_sha256: str

    def artifact_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n"


def migrate_plaintext_v1_ledger(
    source_root: Path,
    destination_root: Path,
    *,
    legacy_integrity_key: bytes,
    ledger_keyring: VersionedKeyring,
    restore_keyring: VersionedKeyring,
    migrated_at: datetime,
) -> LedgerMigrationArtifact:
    """Validate a locked source, build a separate ledger, and publish its active marker last."""

    if migrated_at.tzinfo is None:
        raise TypeError("Ledger migration timestamp must be timezone-aware")
    if len(legacy_integrity_key) < 32:
        raise ValueError("Legacy deletion ledger key must contain at least 32 bytes")
    source = source_root.resolve()
    destination = destination_root.resolve()
    if source == destination:
        raise DeletionLedgerError(
            "LEDGER_MIGRATION_PATH_INVALID",
            "Migration source and destination must be separate directories",
        )
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    if any(destination.iterdir()):
        raise DeletionLedgerError(
            "LEDGER_MIGRATION_DESTINATION_NOT_EMPTY",
            "Migration destination must be empty",
        )
    marker = destination / MIGRATION_MARKER
    staging = destination / f".migration-staging-{secrets.token_hex(8)}"
    _atomic_write(
        marker,
        _canonical(
            {
                "schema_version": "deletion-ledger-migration-marker-v1",
                "started_at": migrated_at.astimezone(UTC).isoformat(),
            }
        )
        + b"\n",
    )
    staging.mkdir(mode=0o700)
    try:
        with _locked_legacy_source(source):
            events, source_sha256 = _read_legacy_events(source, legacy_integrity_key)
            for version in {event.restore_key_version for event in events}:
                try:
                    restore_keyring.require(version)
                except KeyringError as error:
                    raise DeletionLedgerError(
                        "RESTORE_KEY_VERSION_UNAVAILABLE",
                        "Legacy ledger requires an unavailable restore key version",
                    ) from error
            ledger = FilesystemDeletionLedger(
                staging,
                keyring=ledger_keyring,
                restore_key_version=restore_keyring.current_version,
                actor="MIGRATION_CLI",
            )
            ledger.import_legacy_events(
                events,
                source_sha256=source_sha256,
                migrated_at=migrated_at,
            )
            migrated = ledger.events()
            ledger.validate()
            if migrated != events:
                raise DeletionLedgerError(
                    "LEDGER_MIGRATION_VERIFICATION_FAILED",
                    "Migrated ledger does not preserve the exact source event sequence",
                )
            active = cast(
                dict[str, object],
                json.loads((staging / "deletion-ledger-active-v2.json").read_text("ascii")),
            )
            ledger_id = _required_text(active, "ledger_id")
            for filename in (
                "deletion-ledger-v2.jsonl",
                "deletion-ledger-genesis-v2.json",
                "deletion-ledger-head-v2.json",
            ):
                os.replace(staging / filename, destination / filename)
            _fsync_directory(destination)
            os.replace(
                staging / "deletion-ledger-active-v2.json",
                destination / "deletion-ledger-active-v2.json",
            )
            _fsync_directory(destination)
            (staging / ".deletion-ledger.lock").unlink(missing_ok=True)
            staging.rmdir()
            marker.unlink()
            _fsync_directory(destination)
        reopened = FilesystemDeletionLedger(
            destination,
            keyring=ledger_keyring,
            restore_key_version=restore_keyring.current_version,
            actor="MIGRATION_CLI",
            require_existing=True,
        )
        if reopened.events() != events:
            raise DeletionLedgerError(
                "LEDGER_MIGRATION_VERIFICATION_FAILED",
                "Published ledger does not preserve the exact source event sequence",
            )
        return _migration_artifact(
            source_sha256=source_sha256,
            source_event_count=len(events),
            destination_ledger_id=ledger_id,
            destination_head_sha256=_file_sha256(destination / "deletion-ledger-head-v2.json"),
            ledger_key_version=ledger_keyring.current_version,
            restore_key_version=restore_keyring.current_version,
            migrated_at=migrated_at,
        )
    except Exception:
        _fsync_directory(destination)
        raise


def _read_legacy_events(root: Path, integrity_key: bytes) -> tuple[tuple[LedgerEvent, ...], str]:
    stream_path = root / "deletion-ledger-v1.jsonl"
    genesis_path = root / "deletion-ledger-genesis-v1.json"
    try:
        stream = stream_path.read_bytes()
        genesis = genesis_path.read_bytes()
        genesis_payload = cast(dict[str, object], json.loads(genesis.decode("ascii")))
        receipt = _required_text(genesis_payload, "receipt")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise DeletionLedgerError(
            "LEGACY_LEDGER_UNREADABLE",
            "Plaintext v1 deletion ledger cannot be read",
        ) from error
    expected_genesis = hmac.new(
        integrity_key,
        b"RateReplay.DeletionLedgerGenesis.v1\x00",
        hashlib.sha256,
    ).hexdigest()
    if (
        set(genesis_payload) != {"schema_version", "receipt"}
        or genesis_payload.get("schema_version") != "deletion-ledger-genesis-v1"
        or not hmac.compare_digest(receipt, expected_genesis)
    ):
        raise DeletionLedgerError(
            "LEGACY_LEDGER_RECEIPT_INVALID",
            "Plaintext v1 genesis receipt is invalid",
        )
    events: list[LedgerEvent] = []
    try:
        for encoded in stream.splitlines():
            raw = json.loads(encoded.decode("ascii"))
            if not isinstance(raw, dict):
                raise TypeError
            event = LedgerEvent(**raw)
            _validate_event(event)
            if event.schema_version != "deletion-ledger-event-v1":
                raise ValueError
            unsigned = asdict(event)
            unsigned.pop("receipt")
            expected_receipt = hmac.new(
                integrity_key,
                b"RateReplay.DeletionLedgerReceipt.v1\x00" + _canonical(unsigned),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(event.receipt, expected_receipt):
                raise DeletionLedgerError(
                    "LEGACY_LEDGER_RECEIPT_INVALID",
                    "Plaintext v1 event receipt is invalid",
                )
            events.append(event)
    except DeletionLedgerError:
        raise
    except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise DeletionLedgerError(
            "LEGACY_LEDGER_UNREADABLE",
            "Plaintext v1 deletion ledger contains an invalid event",
        ) from error
    _validate_legacy_chains(events)
    source_sha256 = hashlib.sha256(
        b"RateReplay.DeletionLedgerMigrationSource.v1\x00"
        + len(genesis).to_bytes(8, "big")
        + genesis
        + stream
    ).hexdigest()
    return tuple(events), source_sha256


def _validate_legacy_chains(events: list[LedgerEvent]) -> None:
    by_id: dict[str, list[LedgerEvent]] = {}
    for event in events:
        chain = by_id.setdefault(event.deletion_id, [])
        if event.previous_receipt != (chain[-1].receipt if chain else None):
            raise DeletionLedgerError(
                "LEGACY_LEDGER_CHAIN_BROKEN",
                "Plaintext v1 receipt chain is broken",
            )
        chain.append(event)
    if any(tuple(item.phase for item in chain) not in LEGAL_CHAINS for chain in by_id.values()):
        raise DeletionLedgerError(
            "LEGACY_LEDGER_CHAIN_INVALID",
            "Plaintext v1 phase chain is invalid",
        )


class _locked_legacy_source:
    def __init__(self, root: Path) -> None:
        lock_path = root / ".deletion-ledger.lock"
        self._path = lock_path if lock_path.is_file() else root / "deletion-ledger-genesis-v1.json"
        self._lock: BinaryIO | None = None

    def __enter__(self) -> None:
        try:
            self._lock = self._path.open("rb")
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise DeletionLedgerError(
                "LEGACY_LEDGER_LOCK_FAILED",
                "Plaintext v1 ledger cannot be locked for migration",
            ) from error

    def __exit__(self, *_args: object) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()


def _migration_artifact(
    *,
    source_sha256: str,
    source_event_count: int,
    destination_ledger_id: str,
    destination_head_sha256: str,
    ledger_key_version: str,
    restore_key_version: str,
    migrated_at: datetime,
) -> LedgerMigrationArtifact:
    payload: dict[str, object] = {
        "schema_version": "deletion-ledger-migration-artifact-v1",
        "source_sha256": source_sha256,
        "source_event_count": source_event_count,
        "destination_ledger_id": destination_ledger_id,
        "destination_head_sha256": destination_head_sha256,
        "ledger_key_version": ledger_key_version,
        "restore_key_version": restore_key_version,
        "migrated_at": migrated_at.astimezone(UTC).isoformat(),
    }
    return LedgerMigrationArtifact(
        schema_version="deletion-ledger-migration-artifact-v1",
        source_sha256=source_sha256,
        source_event_count=source_event_count,
        destination_ledger_id=destination_ledger_id,
        destination_head_sha256=destination_head_sha256,
        ledger_key_version=ledger_key_version,
        restore_key_version=restore_key_version,
        migrated_at=cast(str, payload["migrated_at"]),
        artifact_sha256=hashlib.sha256(
            b"RateReplay.DeletionLedgerMigrationArtifact.v1\x00" + _canonical(payload)
        ).hexdigest(),
    )


def verify_migration_artifact(payload: Mapping[str, object]) -> LedgerMigrationArtifact:
    try:
        if (
            set(payload)
            != {
                "schema_version",
                "source_sha256",
                "source_event_count",
                "destination_ledger_id",
                "destination_head_sha256",
                "ledger_key_version",
                "restore_key_version",
                "migrated_at",
                "artifact_sha256",
            }
            or payload.get("schema_version") != "deletion-ledger-migration-artifact-v1"
        ):
            raise ValueError
        artifact = LedgerMigrationArtifact(
            schema_version="deletion-ledger-migration-artifact-v1",
            source_sha256=_required_text(payload, "source_sha256"),
            source_event_count=_required_integer(payload, "source_event_count"),
            destination_ledger_id=_required_text(payload, "destination_ledger_id"),
            destination_head_sha256=_required_text(payload, "destination_head_sha256"),
            ledger_key_version=_required_text(payload, "ledger_key_version"),
            restore_key_version=_required_text(payload, "restore_key_version"),
            migrated_at=_required_text(payload, "migrated_at"),
            artifact_sha256=_required_text(payload, "artifact_sha256"),
        )
        unsigned = asdict(artifact)
        digest = cast(str, unsigned.pop("artifact_sha256"))
        expected = hashlib.sha256(
            b"RateReplay.DeletionLedgerMigrationArtifact.v1\x00" + _canonical(unsigned)
        ).hexdigest()
        if (
            artifact.source_event_count < 0
            or len(artifact.source_sha256) != 64
            or len(artifact.destination_head_sha256) != 64
            or datetime.fromisoformat(artifact.migrated_at).tzinfo is None
            or not hmac.compare_digest(digest, expected)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise DeletionLedgerError(
            "LEDGER_MIGRATION_ARTIFACT_INVALID",
            "Deletion ledger migration artifact is invalid",
        ) from error
    return artifact


def write_migration_artifact(path: Path, artifact: LedgerMigrationArtifact) -> None:
    verified = verify_migration_artifact(asdict(artifact))
    try:
        _atomic_write(path, verified.artifact_json().encode("ascii"))
    except OSError as error:
        raise DeletionLedgerError(
            "LEDGER_MIGRATION_ARTIFACT_WRITE_FAILED",
            "Deletion ledger migration artifact could not be persisted",
        ) from error
