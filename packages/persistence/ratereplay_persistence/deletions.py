"""Durable account-deletion preparation, fencing, and receipt coordination."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, TypedDict

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.deletion_ledger import (
    DeletionLedgerError,
    FilesystemDeletionLedger,
    LedgerEvent,
    LedgerPhase,
)
from ratereplay_persistence.models import (
    DeletionControlOperationRecord,
    DeletionIntentRecord,
    DeletionLedgerReceiptRecord,
    DeletionReceiptRecord,
    JobRecord,
    SessionRecord,
    UserRecord,
)

INTENT_LIFETIME: Final = timedelta(minutes=15)
RECEIPT_LIFETIME: Final = timedelta(days=30)
INTENT_SCHEMA: Final = "deletion-intent-v1"
RESTORE_KEY_VERSION: Final = "restore-v1"
_RECEIPT_HASHER = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)


class DeletionServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeletionIntentView:
    deletion_id: str
    status: str
    expires_at: datetime
    repeated: bool


@dataclass(frozen=True, slots=True)
class DeletionStatus:
    deletion_id: str
    status: str
    artifact_counts: dict[str, int]
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    prepared_examined: int
    controls_examined: int
    advanced: int
    quarantined: int


class LedgerAppendArguments(TypedDict):
    deletion_id: str
    phase: LedgerPhase
    scope_token: str
    restore_key_version: str
    original_generation: int
    proposed_generation: int
    preparation_digest: str
    intent_proof_digest: str
    occurred_at: datetime


class DeletionCoordinator:
    """Coordinate the external ledger and exact database lifecycle fences."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        ledger: FilesystemDeletionLedger,
        *,
        restore_key: bytes,
        restore_key_version: str = RESTORE_KEY_VERSION,
    ) -> None:
        if len(restore_key) < 32:
            raise ValueError("Restore suppression key must contain at least 32 bytes")
        if not restore_key_version or len(restore_key_version) > 32:
            raise ValueError("Restore key version is invalid")
        self._session_factory = session_factory
        self._ledger = ledger
        self._restore_key = bytes(restore_key)
        self._restore_key_version = restore_key_version

    def create_intent(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        receipt_secret: bytes,
        now: datetime,
    ) -> DeletionIntentView:
        now = _aware(now)
        _validate_receipt_secret(receipt_secret)
        if not idempotency_key or len(idempotency_key) > 128:
            raise DeletionServiceError(
                "INVALID_IDEMPOTENCY_KEY",
                "Deletion idempotency key is invalid",
            )
        receipt_digest = _receipt_digest(receipt_secret)
        payload_hash = _intent_payload_hash(receipt_digest)
        with self._session_factory.begin() as database:
            user = database.scalar(
                select(UserRecord).where(UserRecord.id == owner_user_id).with_for_update()
            )
            if user is None or user.lifecycle_state != "ACTIVE":
                raise DeletionServiceError(
                    "ACCOUNT_NOT_ACTIVE",
                    "The account cannot start a deletion intent",
                )
            keyed = database.scalar(
                select(DeletionIntentRecord).where(
                    DeletionIntentRecord.owner_user_id == owner_user_id,
                    DeletionIntentRecord.idempotency_key == idempotency_key,
                )
            )
            if keyed is not None:
                if not hmac.compare_digest(keyed.canonical_payload_hash, payload_hash):
                    raise DeletionServiceError(
                        "IDEMPOTENCY_CONFLICT",
                        "The idempotency key is bound to another deletion proof",
                    )
                if keyed.state == "INVALIDATED" or (
                    keyed.state == "INTENT_CREATED" and now >= _aware(keyed.expires_at)
                ):
                    raise DeletionServiceError(
                        "INTENT_EXPIRED",
                        "The deletion intent has expired",
                    )
                return DeletionIntentView(
                    keyed.deletion_id,
                    _receipt_status(database, keyed.deletion_id),
                    _aware(keyed.expires_at),
                    True,
                )
            active = database.scalar(
                select(DeletionIntentRecord).where(
                    DeletionIntentRecord.owner_user_id == owner_user_id,
                    DeletionIntentRecord.state != "INVALIDATED",
                )
            )
            if active is not None:
                if active.state == "INTENT_CREATED" and now >= _aware(active.expires_at):
                    active.state = "INVALIDATED"
                    active.invalidated_at = now
                    database.flush()
                else:
                    raise DeletionServiceError(
                        "DELETION_ALREADY_PENDING",
                        "Another deletion is already pending for this account",
                    )
            if _has_unresolved_preparation(database, owner_user_id):
                raise DeletionServiceError(
                    "DELETION_ALREADY_PENDING",
                    "A prepared deletion remains unresolved for this account",
                )
            deletion_id = secrets.token_hex(16)
            expires_at = now + INTENT_LIFETIME
            database.add(
                DeletionIntentRecord(
                    deletion_id=deletion_id,
                    owner_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    request_schema_version=INTENT_SCHEMA,
                    canonical_payload_hash=payload_hash,
                    receipt_digest=receipt_digest,
                    original_generation=user.lifecycle_generation,
                    proposed_generation=user.lifecycle_generation + 1,
                    state="INTENT_CREATED",
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            database.add(
                DeletionReceiptRecord(
                    deletion_id=deletion_id,
                    receipt_verifier=_RECEIPT_HASHER.hash(receipt_secret),
                    status="INTENT_CREATED",
                    artifact_counts_json="{}",
                    created_at=now,
                )
            )
            return DeletionIntentView(deletion_id, "INTENT_CREATED", expires_at, False)

    def authorize_and_start(
        self,
        *,
        owner_user_id: str,
        deletion_id: str,
        receipt_secret: bytes,
        now: datetime,
    ) -> DeletionStatus:
        now = _aware(now)
        _validate_receipt_secret(receipt_secret)
        intent = self._authorized_intent(
            owner_user_id=owner_user_id,
            deletion_id=deletion_id,
            receipt_secret=receipt_secret,
            now=now,
        )
        prepared = self._ensure_prepared(intent, now=now)
        self._fence_prepared(prepared, now=now)
        self._ensure_requested(deletion_id, now=now)
        return self.status(
            deletion_id=deletion_id,
            receipt_secret=receipt_secret,
            now=now,
        )

    def status(
        self,
        *,
        deletion_id: str,
        receipt_secret: bytes,
        now: datetime,
    ) -> DeletionStatus:
        now = _aware(now)
        _validate_receipt_secret(receipt_secret)
        with self._session_factory() as database:
            receipt = database.get(DeletionReceiptRecord, deletion_id)
            if receipt is None or not _verify_receipt(receipt.receipt_verifier, receipt_secret):
                raise DeletionServiceError(
                    "INVALID_DELETION_PROOF",
                    "Deletion receipt authorization failed",
                )
            intent = database.get(DeletionIntentRecord, deletion_id)
            if (
                receipt.status == "INTENT_CREATED"
                and intent is not None
                and now >= _aware(intent.expires_at)
                and intent.preparation_receipt is None
            ):
                raise DeletionServiceError("INTENT_EXPIRED", "The deletion intent has expired")
            if receipt.verifier_expires_at is not None and now >= _aware(
                receipt.verifier_expires_at
            ):
                raise DeletionServiceError(
                    "DELETION_RECEIPT_EXPIRED",
                    "The deletion receipt has expired",
                )
            counts = json.loads(receipt.artifact_counts_json)
            if not isinstance(counts, dict) or any(
                not isinstance(key, str) or not isinstance(value, int) or value < 0
                for key, value in counts.items()
            ):
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Deletion receipt counts are invalid",
                )
            return DeletionStatus(
                deletion_id=receipt.deletion_id,
                status=receipt.status,
                artifact_counts=counts,
                completed_at=(
                    _aware(receipt.completed_at) if receipt.completed_at is not None else None
                ),
            )

    def reconcile(self, *, now: datetime) -> ReconciliationResult:
        """Advance provable preparations and fenced controls without account credentials."""

        now = _aware(now)
        prepared_events = self._ledger.unresolved_preparations()
        advanced = 0
        quarantined = 0
        for event in prepared_events:
            try:
                if self._preparation_has_proved_noncommit(event):
                    self._ensure_aborted(event, now=now)
                else:
                    self._fence_prepared(event, now=now)
                    self._ensure_requested(event.deletion_id, now=now)
                advanced += 1
            except (DeletionLedgerError, DeletionServiceError):
                quarantined += 1
        with self._session_factory() as database:
            control_ids = tuple(
                database.scalars(
                    select(DeletionControlOperationRecord.deletion_id).where(
                        DeletionControlOperationRecord.phase == "FENCE"
                    )
                )
            )
        for deletion_id in control_ids:
            try:
                self._ensure_requested(deletion_id, now=now)
                advanced += 1
            except (DeletionLedgerError, DeletionServiceError):
                quarantined += 1
        return ReconciliationResult(
            prepared_examined=len(prepared_events),
            controls_examined=len(control_ids),
            advanced=advanced,
            quarantined=quarantined,
        )

    def prove_noncommit_and_abort(self, *, deletion_id: str, now: datetime) -> None:
        """Abort only after a locked transaction proves the prepared fence never committed."""

        now = _aware(now)
        chain = self._ledger.chain(deletion_id)
        if tuple(event.phase for event in chain) != ("PREPARED",):
            raise DeletionServiceError(
                "ABORT_NOT_PROVABLE",
                "The deletion ledger is not an unresolved preparation",
            )
        prepared = chain[0]
        with self._session_factory.begin() as database:
            intent = database.scalar(
                select(DeletionIntentRecord)
                .where(DeletionIntentRecord.deletion_id == deletion_id)
                .with_for_update()
            )
            if intent is None:
                raise DeletionServiceError(
                    "ABORT_NOT_PROVABLE",
                    "The prepared intent is unavailable for noncommit proof",
                )
            user = database.scalar(
                select(UserRecord).where(UserRecord.id == intent.owner_user_id).with_for_update()
            )
            control = database.get(DeletionControlOperationRecord, deletion_id)
            self._validate_event(prepared, intent)
            if not (
                control is None
                and intent.state in {"INTENT_CREATED", "PREPARED"}
                and user is not None
                and user.lifecycle_state == "ACTIVE"
                and user.lifecycle_generation == intent.original_generation
                and user.deletion_scope_id is None
            ):
                raise DeletionServiceError(
                    "ABORT_NOT_PROVABLE",
                    "The database cannot prove that the prepared fence did not commit",
                )
            intent.state = "INVALIDATED"
            intent.invalidated_at = now
        self._ensure_aborted(prepared, now=now)

    def _ensure_aborted(self, prepared: LedgerEvent, *, now: datetime) -> None:
        if not self._preparation_has_proved_noncommit(prepared):
            raise DeletionServiceError(
                "ABORT_NOT_PROVABLE",
                "The database does not retain a positive noncommit proof",
            )
        chain = self._ledger.chain(prepared.deletion_id)
        aborted = next((event for event in chain if event.phase == "ABORTED"), None)
        if aborted is None:
            aborted = self._ledger.append(
                **_event_arguments(prepared, phase="ABORTED", occurred_at=now)
            )
        with self._session_factory.begin() as database:
            self._store_ledger_receipt(database, aborted)
            receipt = database.get(DeletionReceiptRecord, prepared.deletion_id)
            if receipt is None:
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Deletion receipt is missing",
                )
            receipt.status = "ABORTED"

    def _preparation_has_proved_noncommit(self, prepared: LedgerEvent) -> bool:
        with self._session_factory() as database:
            intent = database.get(DeletionIntentRecord, prepared.deletion_id)
            if intent is None:
                return False
            user = database.get(UserRecord, intent.owner_user_id)
            control = database.get(DeletionControlOperationRecord, prepared.deletion_id)
            try:
                self._validate_event(prepared, intent)
            except DeletionServiceError:
                return False
            return bool(
                intent.state == "INVALIDATED"
                and intent.invalidated_at is not None
                and control is None
                and user is not None
                and user.lifecycle_state == "ACTIVE"
                and user.lifecycle_generation == intent.original_generation
                and user.deletion_scope_id is None
            )

    def _authorized_intent(
        self,
        *,
        owner_user_id: str,
        deletion_id: str,
        receipt_secret: bytes,
        now: datetime,
    ) -> DeletionIntentRecord:
        with self._session_factory() as database:
            intent = database.get(DeletionIntentRecord, deletion_id)
            receipt = database.get(DeletionReceiptRecord, deletion_id)
            if (
                intent is None
                or receipt is None
                or intent.owner_user_id != owner_user_id
                or not _verify_receipt(receipt.receipt_verifier, receipt_secret)
            ):
                raise DeletionServiceError(
                    "INVALID_DELETION_PROOF",
                    "Deletion intent authorization failed",
                )
            if intent.state == "INVALIDATED" or (
                intent.state == "INTENT_CREATED" and now >= _aware(intent.expires_at)
            ):
                raise DeletionServiceError("INTENT_EXPIRED", "The deletion intent has expired")
            database.expunge(intent)
            return intent

    def _ensure_prepared(self, intent: DeletionIntentRecord, *, now: datetime) -> LedgerEvent:
        arguments = self._prepared_arguments(intent, occurred_at=now)
        chain = self._ledger.chain(intent.deletion_id)
        if chain:
            prepared = chain[0]
            self._validate_event(prepared, intent)
        else:
            prepared = self._ledger.append(**arguments)
        with self._session_factory.begin() as database:
            current = database.get(DeletionIntentRecord, intent.deletion_id)
            if current is None:
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Prepared deletion intent is missing",
                )
            self._validate_event(prepared, current)
            self._store_ledger_receipt(database, prepared)
            if current.state == "INTENT_CREATED":
                current.state = "PREPARED"
                current.prepared_at = datetime.fromisoformat(prepared.occurred_at)
                current.preparation_digest = prepared.preparation_digest
                current.preparation_receipt = prepared.receipt
            receipt = database.get(DeletionReceiptRecord, intent.deletion_id)
            if receipt is None:
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Prepared deletion receipt is missing",
                )
            if receipt.status == "INTENT_CREATED":
                receipt.status = "PREPARED"
        return prepared

    def _fence_prepared(self, prepared: LedgerEvent, *, now: datetime) -> None:
        if prepared.phase != "PREPARED":
            raise DeletionServiceError(
                "PREPARATION_INVALID",
                "The database fence requires a PREPARED ledger event",
            )
        with self._session_factory.begin() as database:
            intent = database.scalar(
                select(DeletionIntentRecord)
                .where(DeletionIntentRecord.deletion_id == prepared.deletion_id)
                .with_for_update()
            )
            if intent is None:
                raise DeletionServiceError(
                    "PREPARATION_QUARANTINED",
                    "Prepared intent is not available for exact reconciliation",
                )
            user = database.scalar(
                select(UserRecord).where(UserRecord.id == intent.owner_user_id).with_for_update()
            )
            self._validate_event(prepared, intent)
            self._store_ledger_receipt(database, prepared)
            target_scope_id = _target_scope_id(self._restore_key, prepared.deletion_id)
            control = database.get(DeletionControlOperationRecord, prepared.deletion_id)
            if control is not None:
                if not (
                    control.target_scope_id == target_scope_id
                    and control.scope_token == prepared.scope_token
                    and control.deletion_generation == prepared.proposed_generation
                    and control.preparation_digest == prepared.preparation_digest
                    and control.intent_proof_digest == prepared.intent_proof_digest
                    and user is not None
                    and user.deletion_scope_id == target_scope_id
                    and user.lifecycle_state in {"DELETION_PENDING_LEDGER", "DELETING"}
                    and user.lifecycle_generation == prepared.proposed_generation
                ):
                    raise DeletionServiceError(
                        "DELETION_CONTROL_CORRUPT",
                        "Existing deletion control does not match its preparation",
                    )
                return
            if not (
                user is not None
                and user.lifecycle_state == "ACTIVE"
                and user.lifecycle_generation == prepared.original_generation
                and user.deletion_scope_id is None
                and intent.state in {"INTENT_CREATED", "PREPARED"}
                and intent.consumed_at is None
            ):
                raise DeletionServiceError(
                    "PREPARATION_QUARANTINED",
                    "Prepared deletion cannot be fenced from the live database state",
                )
            user.lifecycle_state = "DELETION_PENDING_LEDGER"
            user.lifecycle_generation = prepared.proposed_generation
            user.deletion_scope_id = target_scope_id
            intent.state = "CONSUMED"
            intent.prepared_at = datetime.fromisoformat(prepared.occurred_at)
            intent.preparation_digest = prepared.preparation_digest
            intent.preparation_receipt = prepared.receipt
            intent.consumed_at = now
            database.add(
                DeletionControlOperationRecord(
                    deletion_id=prepared.deletion_id,
                    target_scope_id=target_scope_id,
                    scope_token=prepared.scope_token,
                    restore_key_version=prepared.restore_key_version,
                    original_generation=prepared.original_generation,
                    deletion_generation=prepared.proposed_generation,
                    preparation_digest=prepared.preparation_digest,
                    intent_proof_digest=prepared.intent_proof_digest,
                    phase="FENCE",
                    artifact_counts_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
            receipt = database.get(DeletionReceiptRecord, prepared.deletion_id)
            if receipt is None:
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Deletion receipt is missing during fencing",
                )
            receipt.status = "DELETION_PENDING_LEDGER"
            for session in database.scalars(
                select(SessionRecord).where(
                    SessionRecord.user_id == user.id,
                    SessionRecord.revoked_at.is_(None),
                )
            ):
                session.revoked_at = now
            for job in database.scalars(
                select(JobRecord).where(
                    JobRecord.owner_user_id == user.id,
                    JobRecord.scope_mode == "ACTIVE_SCOPE",
                    JobRecord.state.in_(("QUEUED", "LEASED", "RUNNING")),
                )
            ):
                job.cancel_requested = True
                if job.state == "QUEUED":
                    job.state = "CANCELLED"
                    job.failure_code = "ACCOUNT_DELETION"
                    job.completed_at = now

    def _ensure_requested(self, deletion_id: str, *, now: datetime) -> None:
        with self._session_factory() as database:
            control = database.get(DeletionControlOperationRecord, deletion_id)
            if control is None:
                raise DeletionServiceError(
                    "PREPARATION_QUARANTINED",
                    "Deletion control is not fenced",
                )
            user = database.scalar(
                select(UserRecord).where(UserRecord.deletion_scope_id == control.target_scope_id)
            )
            if not (
                user is not None
                and user.lifecycle_state in {"DELETION_PENDING_LEDGER", "DELETING"}
                and user.lifecycle_generation == control.deletion_generation
            ):
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Requested deletion target is not at its exact fence",
                )
            arguments: LedgerAppendArguments = {
                "deletion_id": control.deletion_id,
                "phase": "REQUESTED",
                "scope_token": control.scope_token,
                "restore_key_version": control.restore_key_version,
                "original_generation": control.original_generation,
                "proposed_generation": control.deletion_generation,
                "preparation_digest": control.preparation_digest,
                "intent_proof_digest": control.intent_proof_digest,
                "occurred_at": now,
            }
        chain = self._ledger.chain(deletion_id)
        requested = next((event for event in chain if event.phase == "REQUESTED"), None)
        if requested is None:
            requested = self._ledger.append(**arguments)
        with self._session_factory.begin() as database:
            control = database.scalar(
                select(DeletionControlOperationRecord)
                .where(DeletionControlOperationRecord.deletion_id == deletion_id)
                .with_for_update()
            )
            if control is None:
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Deletion control disappeared after REQUESTED",
                )
            user = database.scalar(
                select(UserRecord)
                .where(UserRecord.deletion_scope_id == control.target_scope_id)
                .with_for_update()
            )
            _validate_control_event(requested, control, expected_phase="REQUESTED")
            if not (
                user is not None
                and user.lifecycle_generation == control.deletion_generation
                and user.lifecycle_state in {"DELETION_PENDING_LEDGER", "DELETING"}
            ):
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "REQUESTED cannot transition the exact fenced target",
                )
            self._store_ledger_receipt(database, requested)
            if user.lifecycle_state == "DELETING":
                if control.deletion_job_id is None:
                    raise DeletionServiceError(
                        "DELETION_CONTROL_CORRUPT",
                        "Deleting target has no sweep job",
                    )
                return
            job_id = secrets.token_hex(16)
            request_json = json.dumps(
                {"deletion_id": deletion_id, "schema_version": "account-deletion-v1"},
                sort_keys=True,
                separators=(",", ":"),
            )
            deletion_job = JobRecord(
                id=job_id,
                owner_user_id=user.id,
                kind="DELETION",
                request_schema_version="account-deletion-v1",
                request_hash=hashlib.sha256(request_json.encode("ascii")).hexdigest(),
                scope_mode="DELETING_SCOPE",
                request_json=request_json,
                captured_account_generation=control.deletion_generation,
                state="QUEUED",
                attempt_count=0,
                max_attempts=10,
                fencing_generation=0,
                not_before=now,
                cancel_requested=False,
                created_at=now,
            )
            database.add(deletion_job)
            database.flush()
            user.lifecycle_state = "DELETING"
            control.phase = "DRAIN"
            control.deletion_job_id = job_id
            control.updated_at = now
            receipt = database.get(DeletionReceiptRecord, deletion_id)
            if receipt is None:
                raise DeletionServiceError(
                    "DELETION_CONTROL_CORRUPT",
                    "Deletion receipt is missing after REQUESTED",
                )
            receipt.status = "DELETING"

    def _prepared_arguments(
        self,
        intent: DeletionIntentRecord,
        *,
        occurred_at: datetime,
    ) -> LedgerAppendArguments:
        target_scope_id = _target_scope_id(self._restore_key, intent.deletion_id)
        scope_token = _scope_token(self._restore_key, target_scope_id)
        proof_digest = _intent_proof_digest(intent)
        preparation_digest = _preparation_digest(
            deletion_id=intent.deletion_id,
            scope_token=scope_token,
            restore_key_version=self._restore_key_version,
            original_generation=intent.original_generation,
            proposed_generation=intent.proposed_generation,
            intent_proof_digest=proof_digest,
        )
        return {
            "deletion_id": intent.deletion_id,
            "phase": "PREPARED",
            "scope_token": scope_token,
            "restore_key_version": self._restore_key_version,
            "original_generation": intent.original_generation,
            "proposed_generation": intent.proposed_generation,
            "preparation_digest": preparation_digest,
            "intent_proof_digest": proof_digest,
            "occurred_at": occurred_at,
        }

    def _validate_event(self, event: LedgerEvent, intent: DeletionIntentRecord) -> None:
        expected = self._prepared_arguments(
            intent,
            occurred_at=datetime.fromisoformat(event.occurred_at),
        )
        if event.phase != "PREPARED" or any(
            getattr(event, field) != value
            for field, value in expected.items()
            if field not in {"phase", "occurred_at"}
        ):
            raise DeletionServiceError(
                "PREPARATION_IDENTITY_MISMATCH",
                "Prepared ledger identity does not match the deletion intent",
            )

    @staticmethod
    def _store_ledger_receipt(database: Session, event: LedgerEvent) -> None:
        existing = database.scalar(
            select(DeletionLedgerReceiptRecord).where(
                DeletionLedgerReceiptRecord.deletion_id == event.deletion_id,
                DeletionLedgerReceiptRecord.phase == event.phase,
            )
        )
        if existing is not None:
            _validate_stored_receipt(existing, event)
            return
        try:
            with database.begin_nested():
                database.add(
                    DeletionLedgerReceiptRecord(
                        id=secrets.token_hex(16),
                        deletion_id=event.deletion_id,
                        phase=event.phase,
                        canonical_digest=event.canonical_sha256,
                        integrity_receipt=event.receipt,
                        acknowledged_at=datetime.fromisoformat(event.occurred_at),
                    )
                )
                database.flush()
        except IntegrityError as error:
            existing = database.scalar(
                select(DeletionLedgerReceiptRecord).where(
                    DeletionLedgerReceiptRecord.deletion_id == event.deletion_id,
                    DeletionLedgerReceiptRecord.phase == event.phase,
                )
            )
            if existing is None:
                raise DeletionServiceError(
                    "LEDGER_RECEIPT_CONFLICT",
                    "Ledger acknowledgment conflicted without a recoverable record",
                ) from error
            _validate_stored_receipt(existing, event)


def _validate_receipt_secret(secret: bytes) -> None:
    if len(secret) != 32:
        raise DeletionServiceError(
            "INVALID_DELETION_PROOF",
            "Deletion receipt secret must contain exactly 32 bytes",
        )


def _validate_stored_receipt(
    stored: DeletionLedgerReceiptRecord,
    event: LedgerEvent,
) -> None:
    if not (
        hmac.compare_digest(stored.canonical_digest, event.canonical_sha256)
        and hmac.compare_digest(stored.integrity_receipt, event.receipt)
    ):
        raise DeletionServiceError(
            "LEDGER_RECEIPT_MISMATCH",
            "Stored ledger acknowledgment differs from the external ledger",
        )


def _verify_receipt(verifier: str, secret: bytes) -> bool:
    try:
        return bool(_RECEIPT_HASHER.verify(verifier, secret))
    except VerificationError:
        return False


def _receipt_digest(secret: bytes) -> str:
    return hashlib.sha256(b"RateReplay.DeletionReceipt.v1\x00" + secret).hexdigest()


def _intent_payload_hash(receipt_digest: str) -> str:
    payload = json.dumps(
        {"receipt_digest": receipt_digest, "schema_version": INTENT_SCHEMA},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(b"RateReplay.DeletionIntentPayload.v1\x00" + payload).hexdigest()


def _intent_proof_digest(intent: DeletionIntentRecord) -> str:
    payload = json.dumps(
        {
            "canonical_payload_hash": intent.canonical_payload_hash,
            "deletion_id": intent.deletion_id,
            "original_generation": intent.original_generation,
            "owner_user_id": intent.owner_user_id,
            "receipt_digest": intent.receipt_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(b"RateReplay.DeletionIntentProof.v1\x00" + payload).hexdigest()


def _preparation_digest(
    *,
    deletion_id: str,
    scope_token: str,
    restore_key_version: str,
    original_generation: int,
    proposed_generation: int,
    intent_proof_digest: str,
) -> str:
    payload = json.dumps(
        {
            "deletion_id": deletion_id,
            "intent_proof_digest": intent_proof_digest,
            "original_generation": original_generation,
            "proposed_generation": proposed_generation,
            "restore_key_version": restore_key_version,
            "scope_token": scope_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(b"RateReplay.DeletionPreparation.v1\x00" + payload).hexdigest()


def _target_scope_id(restore_key: bytes, deletion_id: str) -> str:
    return hmac.new(
        restore_key,
        b"RateReplay.DeletionTargetScope.v1\x00" + deletion_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _scope_token(restore_key: bytes, target_scope_id: str) -> str:
    return hmac.new(
        restore_key,
        b"RateReplay.RestoreSuppressionScope.v1\x00" + target_scope_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _validate_control_event(
    event: LedgerEvent,
    control: DeletionControlOperationRecord,
    *,
    expected_phase: LedgerPhase,
) -> None:
    if not (
        event.phase == expected_phase
        and event.deletion_id == control.deletion_id
        and event.scope_token == control.scope_token
        and event.restore_key_version == control.restore_key_version
        and event.original_generation == control.original_generation
        and event.proposed_generation == control.deletion_generation
        and event.preparation_digest == control.preparation_digest
        and event.intent_proof_digest == control.intent_proof_digest
    ):
        raise DeletionServiceError(
            "LEDGER_IDENTITY_MISMATCH",
            "Ledger event does not match the deletion control identity",
        )


def _event_arguments(
    event: LedgerEvent,
    *,
    phase: Literal["REQUESTED", "COMPLETED", "ABORTED"],
    occurred_at: datetime,
) -> LedgerAppendArguments:
    return {
        "deletion_id": event.deletion_id,
        "phase": phase,
        "scope_token": event.scope_token,
        "restore_key_version": event.restore_key_version,
        "original_generation": event.original_generation,
        "proposed_generation": event.proposed_generation,
        "preparation_digest": event.preparation_digest,
        "intent_proof_digest": event.intent_proof_digest,
        "occurred_at": occurred_at,
    }


def _receipt_status(database: Session, deletion_id: str) -> str:
    receipt = database.get(DeletionReceiptRecord, deletion_id)
    if receipt is None:
        raise DeletionServiceError(
            "DELETION_CONTROL_CORRUPT",
            "Deletion receipt is missing",
        )
    return receipt.status


def _has_unresolved_preparation(database: Session, owner_user_id: str) -> bool:
    prepared_ids = database.scalars(
        select(DeletionIntentRecord.deletion_id).where(
            DeletionIntentRecord.owner_user_id == owner_user_id,
            DeletionIntentRecord.preparation_receipt.is_not(None),
        )
    ).all()
    if not prepared_ids:
        return False
    terminal_ids = set(
        database.scalars(
            select(DeletionLedgerReceiptRecord.deletion_id).where(
                DeletionLedgerReceiptRecord.deletion_id.in_(prepared_ids),
                DeletionLedgerReceiptRecord.phase.in_(("REQUESTED", "ABORTED")),
            )
        )
    )
    return any(deletion_id not in terminal_ids for deletion_id in prepared_ids)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
