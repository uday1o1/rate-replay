"""Separately stored append-only deletion ledger with keyed integrity receipts."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final, Literal

LedgerPhase = Literal["PREPARED", "REQUESTED", "COMPLETED", "ABORTED"]
LEGAL_CHAINS: Final = {
    ("PREPARED",),
    ("PREPARED", "REQUESTED"),
    ("PREPARED", "REQUESTED", "COMPLETED"),
    ("PREPARED", "ABORTED"),
}


class DeletionLedgerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    schema_version: Literal["deletion-ledger-event-v1"]
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
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_without_receipt()).hexdigest()


class FilesystemDeletionLedger:
    """Local reproducible ledger kept outside the primary database backup scope."""

    def __init__(
        self,
        root: Path,
        *,
        integrity_key: bytes,
        require_existing: bool = False,
    ) -> None:
        if len(integrity_key) < 32:
            raise ValueError("Deletion ledger integrity key must contain at least 32 bytes")
        self._root = root.resolve()
        self._ledger_path = self._root / "deletion-ledger-v1.jsonl"
        self._genesis_path = self._root / "deletion-ledger-genesis-v1.json"
        self._lock_path = self._root / ".deletion-ledger.lock"
        self._integrity_key = bytes(integrity_key)
        if require_existing and not (self._ledger_path.is_file() and self._genesis_path.is_file()):
            raise DeletionLedgerError(
                "LEDGER_MISSING",
                "Deletion ledger or its keyed genesis record is missing",
            )
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        if not require_existing:
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
        with self._locked_events() as events:
            chain = tuple(event for event in events if event.deletion_id == deletion_id)
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
            )
            self._append_line(event)
            events.append(event)
            return event

    def events(self) -> tuple[LedgerEvent, ...]:
        with self._locked_events() as events:
            return tuple(events)

    def chain(self, deletion_id: str) -> tuple[LedgerEvent, ...]:
        return tuple(event for event in self.events() if event.deletion_id == deletion_id)

    def unresolved_preparations(self) -> tuple[LedgerEvent, ...]:
        by_id: dict[str, list[LedgerEvent]] = {}
        for event in self.events():
            by_id.setdefault(event.deletion_id, []).append(event)
        return tuple(
            chain[0]
            for chain in by_id.values()
            if tuple(event.phase for event in chain) == ("PREPARED",)
        )

    def validate(self) -> None:
        self.events()

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
    ) -> LedgerEvent:
        unsigned = LedgerEvent(
            schema_version="deletion-ledger-event-v1",
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
            self._integrity_key,
            b"RateReplay.DeletionLedgerReceipt.v1\x00" + unsigned.canonical_without_receipt(),
            hashlib.sha256,
        ).hexdigest()
        return LedgerEvent(**{**asdict(unsigned), "receipt": receipt})

    def _read_events(self) -> list[LedgerEvent]:
        self._validate_genesis()
        events: list[LedgerEvent] = []
        try:
            lines = self._ledger_path.read_text(encoding="ascii").splitlines()
            for line in lines:
                event = LedgerEvent(**json.loads(line))
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
                )
                if not hmac.compare_digest(expected.receipt, event.receipt):
                    raise DeletionLedgerError(
                        "LEDGER_RECEIPT_INVALID",
                        "Deletion ledger integrity receipt is invalid",
                    )
                events.append(event)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise DeletionLedgerError(
                "LEDGER_UNREADABLE",
                "Deletion ledger cannot be verified",
            ) from error
        by_id: dict[str, list[LedgerEvent]] = {}
        for event in events:
            chain = by_id.setdefault(event.deletion_id, [])
            if event.previous_receipt != (chain[-1].receipt if chain else None):
                raise DeletionLedgerError(
                    "LEDGER_CHAIN_BROKEN",
                    "Deletion ledger receipt chain is broken",
                )
            chain.append(event)
        if any(tuple(item.phase for item in chain) not in LEGAL_CHAINS for chain in by_id.values()):
            raise DeletionLedgerError(
                "ILLEGAL_LEDGER_CHAIN",
                "Deletion ledger contains an illegal phase chain",
            )
        return events

    def _initialize(self) -> None:
        ledger_descriptor = os.open(
            self._ledger_path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        try:
            os.fsync(ledger_descriptor)
        finally:
            os.close(ledger_descriptor)
        if not self._genesis_path.exists():
            receipt = hmac.new(
                self._integrity_key,
                b"RateReplay.DeletionLedgerGenesis.v1\x00",
                hashlib.sha256,
            ).hexdigest()
            payload = json.dumps(
                {
                    "schema_version": "deletion-ledger-genesis-v1",
                    "receipt": receipt,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            temporary = self._root / f".genesis.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, payload + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self._genesis_path)
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _validate_genesis(self) -> None:
        try:
            payload = json.loads(self._genesis_path.read_text(encoding="ascii"))
            receipt = payload["receipt"]
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise DeletionLedgerError(
                "LEDGER_UNREADABLE",
                "Deletion ledger genesis record cannot be verified",
            ) from error
        expected = hmac.new(
            self._integrity_key,
            b"RateReplay.DeletionLedgerGenesis.v1\x00",
            hashlib.sha256,
        ).hexdigest()
        if (
            payload.get("schema_version") != "deletion-ledger-genesis-v1"
            or not isinstance(receipt, str)
            or not hmac.compare_digest(receipt, expected)
        ):
            raise DeletionLedgerError(
                "LEDGER_RECEIPT_INVALID",
                "Deletion ledger genesis integrity receipt is invalid",
            )

    def _append_line(self, event: LedgerEvent) -> None:
        payload = (
            json.dumps(
                asdict(event),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            + b"\n"
        )
        descriptor = os.open(
            self._ledger_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    class _LockedEvents:
        def __init__(self, ledger: FilesystemDeletionLedger) -> None:
            self._ledger = ledger
            self._lock: BinaryIO | None = None

        def __enter__(self) -> list[LedgerEvent]:
            self._lock = self._ledger._lock_path.open("a+b")
            os.chmod(self._ledger._lock_path, 0o600)
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._ledger._read_events()
            except Exception:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
                self._lock.close()
                self._lock = None
                raise

        def __exit__(self, *_args: object) -> None:
            lock = self._lock
            if lock is None:
                raise RuntimeError("Deletion ledger lock invariant failed")
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def _locked_events(self) -> FilesystemDeletionLedger._LockedEvents:
        return self._LockedEvents(self)
