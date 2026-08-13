"""Executable deletion intent and ledger protocol specification."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


class DeletionProtocolError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LifecycleState(StrEnum):
    ACTIVE = "ACTIVE"
    DELETION_PENDING_LEDGER = "DELETION_PENDING_LEDGER"
    DELETING = "DELETING"
    DELETED = "DELETED"


class LedgerPhase(StrEnum):
    PREPARED = "PREPARED"
    REQUESTED = "REQUESTED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class DeletionProtocolState:
    lifecycle: LifecycleState = LifecycleState.ACTIVE
    original_generation: int = 0
    generation: int = 0
    ledger_chain: tuple[LedgerPhase, ...] = ()
    intent_consumed: bool = False
    sweep_checkpoint: str | None = None

    def append_prepared(self) -> DeletionProtocolState:
        if self.lifecycle is not LifecycleState.ACTIVE or self.ledger_chain:
            raise DeletionProtocolError("PREPARE_NOT_ALLOWED")
        return replace(self, ledger_chain=(LedgerPhase.PREPARED,))

    def fence_database(self) -> DeletionProtocolState:
        if (
            self.ledger_chain != (LedgerPhase.PREPARED,)
            or self.lifecycle is not LifecycleState.ACTIVE
        ):
            raise DeletionProtocolError("FENCE_REQUIRES_PREPARED")
        return replace(
            self,
            lifecycle=LifecycleState.DELETION_PENDING_LEDGER,
            generation=self.original_generation + 1,
            intent_consumed=True,
            sweep_checkpoint="FENCE",
        )

    def append_requested(self) -> DeletionProtocolState:
        if (
            self.ledger_chain != (LedgerPhase.PREPARED,)
            or self.lifecycle is not LifecycleState.DELETION_PENDING_LEDGER
            or not self.intent_consumed
        ):
            raise DeletionProtocolError("REQUEST_REQUIRES_FENCE")
        return replace(self, ledger_chain=(*self.ledger_chain, LedgerPhase.REQUESTED))

    def begin_sweep(self) -> DeletionProtocolState:
        if (
            self.ledger_chain != (LedgerPhase.PREPARED, LedgerPhase.REQUESTED)
            or self.lifecycle is not LifecycleState.DELETION_PENDING_LEDGER
        ):
            raise DeletionProtocolError("SWEEP_REQUIRES_REQUESTED")
        return replace(self, lifecycle=LifecycleState.DELETING, sweep_checkpoint="DRAIN")

    def checkpoint(self, phase: str) -> DeletionProtocolState:
        allowed = {"FENCE", "DRAIN", "SWEEP", "VERIFY", "COMPLETE"}
        if self.lifecycle is not LifecycleState.DELETING or phase not in allowed:
            raise DeletionProtocolError("INVALID_SWEEP_CHECKPOINT")
        return replace(self, sweep_checkpoint=phase)

    def append_completed(self) -> DeletionProtocolState:
        if (
            self.lifecycle is not LifecycleState.DELETING
            or self.sweep_checkpoint != "VERIFY"
            or self.ledger_chain != (LedgerPhase.PREPARED, LedgerPhase.REQUESTED)
        ):
            raise DeletionProtocolError("COMPLETE_REQUIRES_VERIFIED_SWEEP")
        return replace(self, ledger_chain=(*self.ledger_chain, LedgerPhase.COMPLETED))

    def finalize_deleted(self) -> DeletionProtocolState:
        if (
            self.ledger_chain[-1:] != (LedgerPhase.COMPLETED,)
            or self.lifecycle is not LifecycleState.DELETING
        ):
            raise DeletionProtocolError("FINALIZE_REQUIRES_COMPLETED")
        return replace(self, lifecycle=LifecycleState.DELETED, sweep_checkpoint=None)

    def append_aborted(self, *, authoritative_noncommit: bool) -> DeletionProtocolState:
        if (
            self.ledger_chain != (LedgerPhase.PREPARED,)
            or self.lifecycle is not LifecycleState.ACTIVE
            or not authoritative_noncommit
        ):
            raise DeletionProtocolError("ABORT_REQUIRES_PROVED_NONCOMMIT")
        return replace(self, ledger_chain=(*self.ledger_chain, LedgerPhase.ABORTED))

    @property
    def restore_exposure_allowed(self) -> bool:
        if self.lifecycle is not LifecycleState.ACTIVE:
            return False
        return not self.ledger_chain or self.ledger_chain[-1] is LedgerPhase.ABORTED

    def validate(self) -> None:
        suppressive = (
            LedgerPhase.REQUESTED in self.ledger_chain or LedgerPhase.COMPLETED in self.ledger_chain
        )
        if suppressive and self.lifecycle is LifecycleState.ACTIVE:
            raise DeletionProtocolError("SUPPRESSIVE_EVENT_WITH_ACTIVE_TARGET")
        if self.ledger_chain[:1] and self.ledger_chain[0] is not LedgerPhase.PREPARED:
            raise DeletionProtocolError("PREPARED_MUST_BE_FIRST")
        if LedgerPhase.ABORTED in self.ledger_chain and LedgerPhase.REQUESTED in self.ledger_chain:
            raise DeletionProtocolError("ILLEGAL_LEDGER_CHAIN")


_RECEIPT_HASHER = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)


@dataclass(frozen=True, slots=True)
class DeletionIntent:
    deletion_id: str
    owner_id: str
    idempotency_key: str
    receipt_digest: str
    receipt_verifier: str
    created_at: datetime
    expires_at: datetime
    consumed: bool = False
    prepared: bool = False
    session_revoked: bool = False
    status: str = "INTENT_CREATED"

    def verify_receipt(self, receipt_secret: bytes) -> bool:
        try:
            return _RECEIPT_HASHER.verify(self.receipt_verifier, receipt_secret)
        except VerifyMismatchError:
            return False


class DeletionIntentRegistry:
    """In-memory protocol model used as the executable Milestone 0 specification."""

    def __init__(self) -> None:
        self._by_owner: dict[str, DeletionIntent] = {}
        self._by_id: dict[str, DeletionIntent] = {}

    @staticmethod
    def _receipt_digest(secret: bytes) -> str:
        return hashlib.sha256(b"RateReplay.DeletionReceipt.v1\x00" + secret).hexdigest()

    def create(
        self,
        *,
        owner_id: str,
        idempotency_key: str,
        receipt_secret: bytes,
        now: datetime,
    ) -> DeletionIntent:
        if now.tzinfo is None:
            raise TypeError("Timestamp must be timezone-aware")
        digest = self._receipt_digest(receipt_secret)
        current = self._by_owner.get(owner_id)
        if current is not None:
            same_request = current.idempotency_key == idempotency_key and hmac.compare_digest(
                current.receipt_digest, digest
            )
            if same_request:
                if now >= current.expires_at and not current.prepared:
                    raise DeletionProtocolError("INTENT_EXPIRED")
                return current
            if current.prepared or now < current.expires_at:
                raise DeletionProtocolError("DELETION_ALREADY_PENDING")
        deletion_id = hashlib.sha256(
            b"RateReplay.DeletionId.v1\x00"
            + owner_id.encode()
            + b"\x00"
            + idempotency_key.encode()
            + b"\x00"
            + digest.encode()
        ).hexdigest()[:32]
        intent = DeletionIntent(
            deletion_id=deletion_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            receipt_digest=digest,
            receipt_verifier=_RECEIPT_HASHER.hash(receipt_secret),
            created_at=now.astimezone(UTC),
            expires_at=now.astimezone(UTC) + timedelta(minutes=15),
        )
        self._by_owner[owner_id] = intent
        self._by_id[deletion_id] = intent
        return intent

    def consume(
        self,
        *,
        deletion_id: str,
        owner_id: str,
        receipt_secret: bytes,
        now: datetime,
    ) -> DeletionIntent:
        intent = self._by_id.get(deletion_id)
        if (
            intent is None
            or intent.owner_id != owner_id
            or not intent.verify_receipt(receipt_secret)
        ):
            raise DeletionProtocolError("INVALID_DELETION_PROOF")
        if now >= intent.expires_at and not intent.prepared:
            raise DeletionProtocolError("INTENT_EXPIRED")
        if intent.consumed:
            return intent
        consumed = replace(
            intent,
            consumed=True,
            prepared=True,
            session_revoked=True,
            status="DELETION_PENDING_LEDGER",
        )
        self._by_owner[owner_id] = consumed
        self._by_id[deletion_id] = consumed
        return consumed

    def status(self, *, deletion_id: str, receipt_secret: bytes) -> str:
        intent = self._by_id.get(deletion_id)
        if intent is None or not intent.verify_receipt(receipt_secret):
            raise DeletionProtocolError("INVALID_DELETION_PROOF")
        return intent.status
