"""Encrypted, access-audited deletion ledger for local reproducible operation."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final, Literal, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ratereplay_persistence.keyrings import KeyringError, VersionedKeyring

LedgerPhase = Literal["PREPARED", "REQUESTED", "COMPLETED", "ABORTED"]
LedgerActor = Literal[
    "API_COORDINATOR",
    "PREPARATION_RECONCILER",
    "DELETION_WORKER",
    "RESTORE_QUALIFIER",
    "RETENTION_WORKER",
    "ROTATION_CLI",
    "MIGRATION_CLI",
    "TEST",
]
LedgerOperation = Literal[
    "APPEND_PREPARED",
    "APPEND_REQUESTED",
    "APPEND_COMPLETED",
    "APPEND_ABORTED",
    "READ_EVENTS",
    "READ_CHAIN",
    "ENUMERATE_UNRESOLVED",
    "VALIDATE",
]
RecordType = Literal["LEDGER_EVENT", "ACCESS_AUDIT"]

LEGAL_CHAINS: Final = {
    ("PREPARED",),
    ("PREPARED", "REQUESTED"),
    ("PREPARED", "REQUESTED", "COMPLETED"),
    ("PREPARED", "ABORTED"),
}
ALLOWED_ACTORS: Final = frozenset(
    {
        "API_COORDINATOR",
        "PREPARATION_RECONCILER",
        "DELETION_WORKER",
        "RESTORE_QUALIFIER",
        "RETENTION_WORKER",
        "ROTATION_CLI",
        "MIGRATION_CLI",
        "TEST",
    }
)
ALLOWED_OPERATIONS: Final = frozenset(
    {
        "APPEND_PREPARED",
        "APPEND_REQUESTED",
        "APPEND_COMPLETED",
        "APPEND_ABORTED",
        "READ_EVENTS",
        "READ_CHAIN",
        "ENUMERATE_UNRESOLVED",
        "VALIDATE",
    }
)
RECORD_KEYS: Final = frozenset(
    {
        "schema_version",
        "ledger_id",
        "sequence",
        "record_type",
        "key_version",
        "nonce",
        "previous_record_sha256",
        "ciphertext",
        "record_sha256",
    }
)
HEADER_KEYS: Final = frozenset(RECORD_KEYS - {"ciphertext", "record_sha256"})
HKDF_SALT: Final = b"RateReplay.DeletionLedger.HKDF.v2\x00"
AAD_DOMAIN: Final = b"RateReplay.DeletionLedger.RecordAAD.v2\x00"
MAXIMUM_NONCE_ATTEMPTS: Final = 8


class DeletionLedgerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    schema_version: Literal["deletion-ledger-event-v2"]
    deletion_id: str
    phase: LedgerPhase
    scope_token: str
    restore_key_version: str
    original_generation: int
    proposed_generation: int
    preparation_digest: str
    intent_proof_digest: str
    occurred_at: str
    previous_receipt: str | None
    receipt: str

    def canonical_without_receipt(self) -> bytes:
        payload = asdict(self)
        payload.pop("receipt")
        return _canonical(payload)

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_without_receipt()).hexdigest()


@dataclass(slots=True)
class _LedgerState:
    ledger_id: str
    events: list[LedgerEvent]
    last_sequence: int
    last_record_sha256: str | None
    nonces: set[tuple[str, str]]


class FilesystemDeletionLedger:
    """Encrypted filesystem adapter scoped only to ``LOCAL_REPRODUCIBLE``."""

    def __init__(
        self,
        root: Path,
        *,
        integrity_key: bytes | None = None,
        keyring: VersionedKeyring | None = None,
        restore_key_version: str = "restore-v1",
        actor: LedgerActor = "TEST",
        require_existing: bool = False,
    ) -> None:
        if (integrity_key is None) == (keyring is None):
            raise ValueError("Provide exactly one deletion ledger key source")
        if integrity_key is not None:
            if len(integrity_key) != 32:
                raise ValueError("Deletion ledger key must contain exactly 32 bytes")
            keyring = VersionedKeyring.single("ledger-v1", bytes(integrity_key))
        if keyring is None:
            raise RuntimeError("Deletion ledger key source invariant failed")
        if actor not in ALLOWED_ACTORS:
            raise ValueError("Deletion ledger actor is invalid")
        if not restore_key_version or len(restore_key_version) > 64:
            raise ValueError("Restore key version is invalid")

        self._root = root.resolve()
        self._stream_path = self._root / "deletion-ledger-v2.jsonl"
        self._genesis_path = self._root / "deletion-ledger-genesis-v2.json"
        self._head_path = self._root / "deletion-ledger-head-v2.json"
        self._active_path = self._root / "deletion-ledger-active-v2.json"
        self._lock_path = self._root / ".deletion-ledger.lock"
        self._legacy_stream_path = self._root / "deletion-ledger-v1.jsonl"
        self._legacy_genesis_path = self._root / "deletion-ledger-genesis-v1.json"
        self._keyring = keyring
        self._restore_key_version = restore_key_version
        self._actor = actor

        if not self._active_path.is_file() and (
            self._legacy_stream_path.exists() or self._legacy_genesis_path.exists()
        ):
            raise DeletionLedgerError(
                "LEDGER_FORMAT_MIGRATION_REQUIRED",
                "Plaintext deletion ledger requires an explicit offline migration",
            )
        required = (
            self._active_path,
            self._genesis_path,
            self._stream_path,
            self._head_path,
        )
        if require_existing and not all(path.is_file() for path in required):
            raise DeletionLedgerError(
                "LEDGER_MISSING",
                "Encrypted deletion ledger control files are missing",
            )
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        if not require_existing:
            with self._locked():
                if not self._active_path.exists():
                    if any(path.exists() for path in required[1:]):
                        raise DeletionLedgerError(
                            "LEDGER_INITIALIZATION_INCOMPLETE",
                            "Deletion ledger has incomplete v2 control files",
                        )
                    self._initialize()

    def append(
        self,
        *,
        deletion_id: str,
        phase: LedgerPhase,
        scope_token: str,
        restore_key_version: str,
        original_generation: int,
        proposed_generation: int,
        preparation_digest: str,
        intent_proof_digest: str,
        occurred_at: datetime,
    ) -> LedgerEvent:
        if occurred_at.tzinfo is None:
            raise TypeError("Ledger event timestamp must be timezone-aware")
        operation = cast(LedgerOperation, f"APPEND_{phase}")
        with self._locked():
            state = self._read_state(recover_head=True)
            self._audit(state, operation)
            chain = tuple(event for event in state.events if event.deletion_id == deletion_id)
            fixed = (
                scope_token,
                restore_key_version,
                original_generation,
                proposed_generation,
                preparation_digest,
                intent_proof_digest,
            )
            if chain:
                first = chain[0]
                if fixed != (
                    first.scope_token,
                    first.restore_key_version,
                    first.original_generation,
                    first.proposed_generation,
                    first.preparation_digest,
                    first.intent_proof_digest,
                ):
                    raise DeletionLedgerError(
                        "LEDGER_IDENTITY_MISMATCH",
                        "Deletion ledger identity differs from the prepared event",
                    )
                existing = next((event for event in chain if event.phase == phase), None)
                if existing is not None:
                    candidate = self._event(
                        deletion_id=deletion_id,
                        phase=phase,
                        scope_token=scope_token,
                        restore_key_version=restore_key_version,
                        original_generation=original_generation,
                        proposed_generation=proposed_generation,
                        preparation_digest=preparation_digest,
                        intent_proof_digest=intent_proof_digest,
                        occurred_at=occurred_at,
                        previous_receipt=existing.previous_receipt,
                        receipt_key_version=self._keyring.current_version,
                    )
                    if (
                        candidate.canonical_without_receipt()
                        != existing.canonical_without_receipt()
                    ):
                        raise DeletionLedgerError(
                            "LEDGER_DUPLICATE_MISMATCH",
                            "Duplicate ledger event differs from its canonical record",
                        )
                    return existing
            phases = (*(event.phase for event in chain), phase)
            if phases not in LEGAL_CHAINS:
                raise DeletionLedgerError(
                    "ILLEGAL_LEDGER_CHAIN",
                    "Deletion ledger phase transition is not allowed",
                )
            event = self._event(
                deletion_id=deletion_id,
                phase=phase,
                scope_token=scope_token,
                restore_key_version=restore_key_version,
                original_generation=original_generation,
                proposed_generation=proposed_generation,
                preparation_digest=preparation_digest,
                intent_proof_digest=intent_proof_digest,
                occurred_at=occurred_at,
                previous_receipt=chain[-1].receipt if chain else None,
                receipt_key_version=self._keyring.current_version,
            )
            self._append_record(
                state,
                "LEDGER_EVENT",
                {
                    "schema_version": "deletion-ledger-event-payload-v2",
                    "receipt_key_version": self._keyring.current_version,
                    "event": asdict(event),
                },
            )
            state.events.append(event)
            return event

    def events(self) -> tuple[LedgerEvent, ...]:
        with self._locked():
            state = self._read_state(recover_head=True)
            self._audit(state, "READ_EVENTS")
            return tuple(state.events)

    def chain(self, deletion_id: str) -> tuple[LedgerEvent, ...]:
        with self._locked():
            state = self._read_state(recover_head=True)
            self._audit(state, "READ_CHAIN")
            return tuple(event for event in state.events if event.deletion_id == deletion_id)

    def unresolved_preparations(self) -> tuple[LedgerEvent, ...]:
        with self._locked():
            state = self._read_state(recover_head=True)
            self._audit(state, "ENUMERATE_UNRESOLVED")
            by_id: dict[str, list[LedgerEvent]] = {}
            for event in state.events:
                by_id.setdefault(event.deletion_id, []).append(event)
            return tuple(
                chain[0]
                for chain in by_id.values()
                if tuple(event.phase for event in chain) == ("PREPARED",)
            )

    def validate(self) -> None:
        with self._locked():
            state = self._read_state(recover_head=True)
            self._audit(state, "VALIDATE")

    def _event(
        self,
        *,
        deletion_id: str,
        phase: LedgerPhase,
        scope_token: str,
        restore_key_version: str,
        original_generation: int,
        proposed_generation: int,
        preparation_digest: str,
        intent_proof_digest: str,
        occurred_at: datetime,
        previous_receipt: str | None,
        receipt_key_version: str,
    ) -> LedgerEvent:
        unsigned = LedgerEvent(
            schema_version="deletion-ledger-event-v2",
            deletion_id=deletion_id,
            phase=phase,
            scope_token=scope_token,
            restore_key_version=restore_key_version,
            original_generation=original_generation,
            proposed_generation=proposed_generation,
            preparation_digest=preparation_digest,
            intent_proof_digest=intent_proof_digest,
            occurred_at=occurred_at.astimezone(UTC).isoformat(),
            previous_receipt=previous_receipt,
            receipt="",
        )
        receipt = hmac.new(
            self._derived_key(receipt_key_version, "event-receipt"),
            b"RateReplay.DeletionLedgerReceipt.v2\x00" + unsigned.canonical_without_receipt(),
            hashlib.sha256,
        ).hexdigest()
        return LedgerEvent(**{**asdict(unsigned), "receipt": receipt})

    def _audit(self, state: _LedgerState, operation: LedgerOperation) -> None:
        try:
            self._append_record(
                state,
                "ACCESS_AUDIT",
                {
                    "schema_version": "deletion-ledger-access-audit-v2",
                    "operation_id": secrets.token_hex(16),
                    "actor": self._actor,
                    "operation": operation,
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "previous_sequence": state.last_sequence,
                    "previous_record_sha256": state.last_record_sha256,
                    "outcome": "AUTHORIZED",
                },
            )
        except (DeletionLedgerError, OSError) as error:
            raise DeletionLedgerError(
                "LEDGER_ACCESS_AUDIT_FAILED",
                "Deletion ledger access audit could not be persisted",
            ) from error

    def _append_record(
        self,
        state: _LedgerState,
        record_type: RecordType,
        payload: Mapping[str, object],
    ) -> None:
        key_version = self._keyring.current_version
        nonce = self._unique_nonce(state, key_version)
        header: dict[str, object] = {
            "schema_version": "deletion-ledger-record-v2",
            "ledger_id": state.ledger_id,
            "sequence": state.last_sequence + 1,
            "record_type": record_type,
            "key_version": key_version,
            "nonce": nonce.hex(),
            "previous_record_sha256": state.last_record_sha256,
        }
        ciphertext = AESGCM(self._derived_key(key_version, "envelope-encryption")).encrypt(
            nonce,
            _canonical(dict(payload)),
            AAD_DOMAIN + _canonical(header),
        )
        unsigned = {**header, "ciphertext": base64.b64encode(ciphertext).decode("ascii")}
        record_sha256 = hashlib.sha256(_canonical(unsigned)).hexdigest()
        record = {**unsigned, "record_sha256": record_sha256}
        try:
            descriptor = os.open(
                self._stream_path,
                os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, _canonical(record) + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise DeletionLedgerError(
                "LEDGER_APPEND_FAILED",
                "Encrypted deletion ledger record could not be persisted",
            ) from error
        state.last_sequence += 1
        state.last_record_sha256 = record_sha256
        state.nonces.add((key_version, nonce.hex()))
        self._write_head(state)

    def _unique_nonce(self, state: _LedgerState, key_version: str) -> bytes:
        for _ in range(MAXIMUM_NONCE_ATTEMPTS):
            nonce = secrets.token_bytes(12)
            if (key_version, nonce.hex()) not in state.nonces:
                return nonce
        raise DeletionLedgerError(
            "LEDGER_NONCE_REUSE",
            "Deletion ledger could not allocate a unique encryption nonce",
        )

    def _read_state(self, *, recover_head: bool) -> _LedgerState:
        active = self._read_signed(self._active_path, "active")
        genesis = self._read_signed(self._genesis_path, "genesis")
        head = self._read_signed(self._head_path, "head")
        ledger_id = _required_text(active, "ledger_id")
        if (
            active.get("schema_version") != "deletion-ledger-active-v2"
            or genesis.get("schema_version") != "deletion-ledger-genesis-v2"
            or head.get("schema_version") != "deletion-ledger-head-v2"
            or genesis.get("ledger_id") != ledger_id
            or head.get("ledger_id") != ledger_id
        ):
            raise DeletionLedgerError(
                "LEDGER_CONTROL_MISMATCH",
                "Deletion ledger control files do not identify the same ledger",
            )
        if head.get("current_ledger_key_version") != self._keyring.current_version:
            raise DeletionLedgerError(
                "LEDGER_KEY_CONFIGURATION_MISMATCH",
                "Configured current ledger key does not match the signed head",
            )
        if head.get("current_restore_key_version") != self._restore_key_version:
            raise DeletionLedgerError(
                "RESTORE_KEY_CONFIGURATION_MISMATCH",
                "Configured current restore key does not match the signed head",
            )

        events: list[LedgerEvent] = []
        nonces: set[tuple[str, str]] = set()
        record_hashes: list[str] = []
        previous_record_sha256: str | None = None
        try:
            lines = self._stream_path.read_text(encoding="ascii").splitlines()
        except (OSError, UnicodeError) as error:
            raise DeletionLedgerError(
                "LEDGER_UNREADABLE",
                "Encrypted deletion ledger stream cannot be read",
            ) from error
        for expected_sequence, line in enumerate(lines, start=1):
            record = self._decode_record(
                line,
                ledger_id=ledger_id,
                expected_sequence=expected_sequence,
                expected_previous=previous_record_sha256,
                nonces=nonces,
            )
            previous_record_sha256 = cast(str, record["record_sha256"])
            record_hashes.append(previous_record_sha256)
            payload = cast(dict[str, object], record["payload"])
            if record["record_type"] == "LEDGER_EVENT":
                events.append(
                    self._decode_event(
                        payload,
                        envelope_key_version=cast(str, record["key_version"]),
                    )
                )
            else:
                self._validate_audit(
                    payload,
                    expected_previous_sequence=cast(int, record["sequence"]) - 1,
                    expected_previous_record_sha256=cast(
                        str | None,
                        record["previous_record_sha256"],
                    ),
                )
        self._validate_event_chains(events)

        head_sequence = _required_integer(head, "last_sequence")
        head_hash = head.get("last_record_sha256")
        if head_sequence < 0 or head_sequence > len(record_hashes):
            raise DeletionLedgerError(
                "LEDGER_HEAD_AHEAD",
                "Signed ledger head is ahead of the encrypted stream",
            )
        expected_head_hash = None if head_sequence == 0 else record_hashes[head_sequence - 1]
        if head_hash != expected_head_hash:
            raise DeletionLedgerError(
                "LEDGER_HEAD_MISMATCH",
                "Signed ledger head does not match the encrypted stream",
            )
        state = _LedgerState(
            ledger_id=ledger_id,
            events=events,
            last_sequence=len(record_hashes),
            last_record_sha256=record_hashes[-1] if record_hashes else None,
            nonces=nonces,
        )
        if len(record_hashes) > head_sequence:
            if not recover_head:
                raise DeletionLedgerError(
                    "LEDGER_HEAD_BEHIND",
                    "Encrypted ledger contains an uncommitted tail",
                )
            self._write_head(state)
        return state

    def _decode_record(
        self,
        line: str,
        *,
        ledger_id: str,
        expected_sequence: int,
        expected_previous: str | None,
        nonces: set[tuple[str, str]],
    ) -> dict[str, object]:
        try:
            record = cast(dict[str, object], json.loads(line))
            if not isinstance(record, dict) or set(record) != RECORD_KEYS:
                raise TypeError
            if record["schema_version"] != "deletion-ledger-record-v2":
                raise TypeError
            if record["ledger_id"] != ledger_id:
                raise DeletionLedgerError(
                    "LEDGER_RECORD_LEDGER_MISMATCH",
                    "Encrypted record belongs to another ledger",
                )
            if record["sequence"] != expected_sequence:
                raise DeletionLedgerError(
                    "LEDGER_SEQUENCE_INVALID",
                    "Encrypted ledger sequence is not contiguous",
                )
            if isinstance(record["sequence"], bool) or not isinstance(record["sequence"], int):
                raise DeletionLedgerError(
                    "LEDGER_SEQUENCE_INVALID",
                    "Encrypted ledger sequence is not an integer",
                )
            if record["previous_record_sha256"] != expected_previous:
                raise DeletionLedgerError(
                    "LEDGER_GLOBAL_CHAIN_BROKEN",
                    "Encrypted ledger global record chain is broken",
                )
            record_type = record["record_type"]
            if record_type not in {"LEDGER_EVENT", "ACCESS_AUDIT"}:
                raise TypeError
            key_version = _required_text(record, "key_version")
            nonce_text = _required_text(record, "nonce")
            nonce = bytes.fromhex(nonce_text)
            if len(nonce) != 12 or nonce_text != nonce.hex():
                raise TypeError
            nonce_identity = (key_version, nonce_text)
            if nonce_identity in nonces:
                raise DeletionLedgerError(
                    "LEDGER_NONCE_REUSE",
                    "Encrypted ledger reuses a nonce under the same key",
                )
            unsigned = {key: record[key] for key in RECORD_KEYS - {"record_sha256"}}
            expected_hash = hashlib.sha256(_canonical(unsigned)).hexdigest()
            record_sha256 = _required_text(record, "record_sha256")
            if not hmac.compare_digest(expected_hash, record_sha256):
                raise DeletionLedgerError(
                    "LEDGER_RECORD_HASH_INVALID",
                    "Encrypted ledger record hash is invalid",
                )
            header = {key: record[key] for key in HEADER_KEYS}
            ciphertext = base64.b64decode(_required_text(record, "ciphertext"), validate=True)
            try:
                plaintext = AESGCM(self._derived_key(key_version, "envelope-encryption")).decrypt(
                    nonce, ciphertext, AAD_DOMAIN + _canonical(header)
                )
            except InvalidTag as error:
                raise DeletionLedgerError(
                    "LEDGER_RECORD_AUTHENTICATION_FAILED",
                    "Encrypted ledger record authentication failed",
                ) from error
            payload = json.loads(plaintext.decode("ascii"))
            if not isinstance(payload, dict):
                raise TypeError
            nonces.add(nonce_identity)
            return {**record, "payload": cast(dict[str, object], payload)}
        except DeletionLedgerError:
            raise
        except KeyringError as error:
            raise DeletionLedgerError(
                "LEDGER_KEY_VERSION_UNAVAILABLE",
                "Encrypted ledger requires an unavailable historical key",
            ) from error
        except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
            raise DeletionLedgerError(
                "LEDGER_UNREADABLE",
                "Encrypted deletion ledger record cannot be decoded",
            ) from error

    def _decode_event(
        self,
        payload: dict[str, object],
        *,
        envelope_key_version: str,
    ) -> LedgerEvent:
        if set(payload) != {"schema_version", "receipt_key_version", "event"}:
            raise DeletionLedgerError("LEDGER_UNREADABLE", "Ledger event payload is invalid")
        if payload["schema_version"] != "deletion-ledger-event-payload-v2":
            raise DeletionLedgerError(
                "LEDGER_UNREADABLE", "Ledger event payload version is invalid"
            )
        receipt_key_version = _required_text(payload, "receipt_key_version")
        if receipt_key_version != envelope_key_version:
            raise DeletionLedgerError(
                "LEDGER_RECEIPT_KEY_MISMATCH",
                "Ledger receipt key does not match its encrypted envelope",
            )
        raw_event = payload["event"]
        if not isinstance(raw_event, dict):
            raise DeletionLedgerError("LEDGER_UNREADABLE", "Ledger event is not an object")
        try:
            event = LedgerEvent(**raw_event)
            _validate_event(event)
            expected = self._event(
                deletion_id=event.deletion_id,
                phase=event.phase,
                scope_token=event.scope_token,
                restore_key_version=event.restore_key_version,
                original_generation=event.original_generation,
                proposed_generation=event.proposed_generation,
                preparation_digest=event.preparation_digest,
                intent_proof_digest=event.intent_proof_digest,
                occurred_at=datetime.fromisoformat(event.occurred_at),
                previous_receipt=event.previous_receipt,
                receipt_key_version=receipt_key_version,
            )
        except (TypeError, ValueError) as error:
            raise DeletionLedgerError("LEDGER_UNREADABLE", "Ledger event is invalid") from error
        if not hmac.compare_digest(expected.receipt, event.receipt):
            raise DeletionLedgerError(
                "LEDGER_RECEIPT_INVALID",
                "Deletion ledger event receipt is invalid",
            )
        return event

    def _validate_audit(
        self,
        payload: dict[str, object],
        *,
        expected_previous_sequence: int,
        expected_previous_record_sha256: str | None,
    ) -> None:
        if set(payload) != {
            "schema_version",
            "operation_id",
            "actor",
            "operation",
            "occurred_at",
            "previous_sequence",
            "previous_record_sha256",
            "outcome",
        }:
            raise DeletionLedgerError("LEDGER_AUDIT_INVALID", "Ledger access audit is invalid")
        if (
            payload["schema_version"] != "deletion-ledger-access-audit-v2"
            or payload["actor"] not in ALLOWED_ACTORS
            or payload["operation"] not in ALLOWED_OPERATIONS
            or payload["outcome"] != "AUTHORIZED"
            or not isinstance(payload["previous_sequence"], int)
            or not isinstance(payload["operation_id"], str)
            or len(payload["operation_id"]) != 32
            or payload["previous_sequence"] != expected_previous_sequence
            or payload["previous_record_sha256"] != expected_previous_record_sha256
        ):
            raise DeletionLedgerError("LEDGER_AUDIT_INVALID", "Ledger access audit is invalid")
        try:
            occurred_at = datetime.fromisoformat(_required_text(payload, "occurred_at"))
        except ValueError as error:
            raise DeletionLedgerError(
                "LEDGER_AUDIT_INVALID", "Ledger access audit is invalid"
            ) from error
        if occurred_at.tzinfo is None:
            raise DeletionLedgerError("LEDGER_AUDIT_INVALID", "Ledger access audit is invalid")

    def _validate_event_chains(self, events: list[LedgerEvent]) -> None:
        by_id: dict[str, list[LedgerEvent]] = {}
        for event in events:
            chain = by_id.setdefault(event.deletion_id, [])
            if event.previous_receipt != (chain[-1].receipt if chain else None):
                raise DeletionLedgerError(
                    "LEDGER_CHAIN_BROKEN",
                    "Deletion ledger event receipt chain is broken",
                )
            chain.append(event)
        if any(
            tuple(event.phase for event in chain) not in LEGAL_CHAINS for chain in by_id.values()
        ):
            raise DeletionLedgerError(
                "ILLEGAL_LEDGER_CHAIN",
                "Deletion ledger contains an illegal event chain",
            )

    def _initialize(self) -> None:
        ledger_id = secrets.token_hex(16)
        key_version = self._keyring.current_version
        created_at = datetime.now(UTC).isoformat()
        descriptor = os.open(self._stream_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._write_signed(
            self._genesis_path,
            {
                "schema_version": "deletion-ledger-genesis-v2",
                "ledger_id": ledger_id,
                "created_at": created_at,
                "ledger_key_version": key_version,
            },
            "genesis",
        )
        state = _LedgerState(ledger_id, [], 0, None, set())
        self._write_head(state)
        self._write_signed(
            self._active_path,
            {
                "schema_version": "deletion-ledger-active-v2",
                "ledger_id": ledger_id,
                "created_at": created_at,
                "format": "ENCRYPTED_GLOBAL_CHAIN",
            },
            "active",
        )
        _fsync_directory(self._root)

    def _write_head(self, state: _LedgerState) -> None:
        self._write_signed(
            self._head_path,
            {
                "schema_version": "deletion-ledger-head-v2",
                "ledger_id": state.ledger_id,
                "last_sequence": state.last_sequence,
                "last_record_sha256": state.last_record_sha256,
                "current_ledger_key_version": self._keyring.current_version,
                "current_restore_key_version": self._restore_key_version,
            },
            "head",
        )

    def _write_signed(self, path: Path, payload: dict[str, object], purpose: str) -> None:
        key_version = self._keyring.current_version
        signed = {**payload, "signing_key_version": key_version}
        signature = hmac.new(
            self._derived_key(key_version, f"{purpose}-hmac"),
            f"RateReplay.DeletionLedger.{purpose}.v2\0".encode("ascii") + _canonical(signed),
            hashlib.sha256,
        ).hexdigest()
        try:
            _atomic_write(path, _canonical({**signed, "hmac_sha256": signature}) + b"\n")
        except OSError as error:
            raise DeletionLedgerError(
                "LEDGER_CONTROL_WRITE_FAILED",
                "Deletion ledger control file could not be persisted",
            ) from error

    def _read_signed(self, path: Path, purpose: str) -> dict[str, object]:
        try:
            payload = cast(dict[str, object], json.loads(path.read_text(encoding="ascii")))
            if not isinstance(payload, dict):
                raise TypeError
            signature = _required_text(payload, "hmac_sha256")
            key_version = _required_text(payload, "signing_key_version")
            unsigned = dict(payload)
            unsigned.pop("hmac_sha256")
            expected = hmac.new(
                self._derived_key(key_version, f"{purpose}-hmac"),
                f"RateReplay.DeletionLedger.{purpose}.v2\0".encode("ascii") + _canonical(unsigned),
                hashlib.sha256,
            ).hexdigest()
        except KeyringError as error:
            raise DeletionLedgerError(
                "LEDGER_KEY_VERSION_UNAVAILABLE",
                "Ledger control file requires an unavailable historical key",
            ) from error
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise DeletionLedgerError(
                "LEDGER_UNREADABLE",
                "Deletion ledger control file cannot be decoded",
            ) from error
        if not hmac.compare_digest(signature, expected):
            raise DeletionLedgerError(
                "LEDGER_SIGNATURE_INVALID",
                "Deletion ledger control-file signature is invalid",
            )
        return payload

    def _derived_key(self, version: str, purpose: str) -> bytes:
        master = self._keyring.require(version)
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=HKDF_SALT,
            info=b"RateReplay.DeletionLedger."
            + purpose.encode("ascii")
            + b".v2\x00"
            + version.encode("ascii"),
        ).derive(master)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock: BinaryIO | None = None
        try:
            lock = self._lock_path.open("a+b")
            os.chmod(self._lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if lock is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()


def _validate_event(event: LedgerEvent) -> None:
    if (
        event.schema_version != "deletion-ledger-event-v2"
        or event.phase not in {"PREPARED", "REQUESTED", "COMPLETED", "ABORTED"}
        or not event.deletion_id
        or not event.scope_token
        or not event.restore_key_version
        or event.original_generation < 0
        or event.proposed_generation <= event.original_generation
        or len(event.preparation_digest) != 64
        or len(event.intent_proof_digest) != 64
        or len(event.receipt) != 64
    ):
        raise ValueError("Ledger event fields are invalid")
    occurred_at = datetime.fromisoformat(event.occurred_at)
    if occurred_at.tzinfo is None:
        raise ValueError("Ledger event timestamp is naive")


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be nonempty text")
    return value


def _required_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
