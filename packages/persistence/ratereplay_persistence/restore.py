"""Network-quarantine restore qualification with deletion-ledger suppression."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.deletion_ledger import (
    DeletionLedgerError,
    FilesystemDeletionLedger,
    LedgerEvent,
)
from ratereplay_persistence.deletion_sweep import (
    _delete_resource_rows,
    _listed_resource_objects,
    _resource_graph,
    _sweep_owner_rows,
)
from ratereplay_persistence.deletions import (
    DeletionCoordinator,
    DeletionServiceError,
    _event_arguments,
    _scope_token,
    _target_by_scope,
)
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.models import (
    DeletionAuditRecord,
    DeletionControlOperationRecord,
    DeletionFenceTargetRecord,
    DeletionIntentRecord,
    DeletionLedgerReceiptRecord,
    DeletionReceiptRecord,
    ImportRecord,
    ProfileVersionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import ObjectStore


class RestoreQualificationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TransactionOutcomeEvidence:
    schema_version: Literal["transaction-outcome-evidence-v1"]
    deletion_id: str
    prepared_receipt: str
    outcome: Literal["COMMITTED", "NOT_COMMITTED"]
    observed_at: str
    authority: str
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

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> TransactionOutcomeEvidence:
        try:
            if payload["schema_version"] != "transaction-outcome-evidence-v1":
                raise RestoreQualificationError(
                    "OUTCOME_EVIDENCE_INVALID",
                    "Transaction outcome evidence schema is unsupported",
                )
            return cls(
                schema_version="transaction-outcome-evidence-v1",
                deletion_id=str(payload["deletion_id"]),
                prepared_receipt=str(payload["prepared_receipt"]),
                outcome=_outcome(str(payload["outcome"])),
                observed_at=str(payload["observed_at"]),
                authority=str(payload["authority"]),
                receipt=str(payload["receipt"]),
            )
        except KeyError as error:
            raise RestoreQualificationError(
                "OUTCOME_EVIDENCE_INVALID",
                "Transaction outcome evidence is missing a required field",
            ) from error


@dataclass(frozen=True, slots=True)
class RestoreQualification:
    schema_version: Literal["restore-qualification-v1"]
    exposure_allowed: bool
    suppressed_deletions: tuple[str, ...]
    requested_deletions: tuple[str, ...]
    aborted_deletions: tuple[str, ...]
    quarantine_holds: tuple[str, ...]
    retention_expired_objects: int
    qualified_at: str
    artifact_sha256: str

    def artifact_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n"


@dataclass(frozen=True, slots=True)
class RestoreTarget:
    kind: Literal["ACCOUNT", "IMPORT", "PROFILE"]
    resource_id: str
    owner_user_id: str
    scope_id: str


class RestoreReconciler:
    """Fail closed until every external deletion event is safe for service exposure."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_store: ObjectStore,
        ledger: FilesystemDeletionLedger,
        *,
        restore_key: bytes,
        restore_key_version: str,
        outcome_evidence_key: bytes,
    ) -> None:
        if len(restore_key) != 32 or len(outcome_evidence_key) != 32:
            raise ValueError("Restore and transaction outcome keys must contain exactly 32 bytes")
        self._session_factory = session_factory
        self._objects = object_store
        self._ledger = ledger
        self._restore_key = bytes(restore_key)
        self._restore_key_version = restore_key_version
        self._outcome_key = bytes(outcome_evidence_key)
        self._coordinator = DeletionCoordinator(
            session_factory,
            ledger,
            restore_key=restore_key,
            restore_key_version=restore_key_version,
        )

    def qualify(
        self,
        *,
        now: datetime,
        outcome_evidence: tuple[TransactionOutcomeEvidence, ...] = (),
    ) -> RestoreQualification:
        now = _aware(now)
        try:
            self._ledger.validate()
            events = self._ledger.events()
        except DeletionLedgerError as error:
            raise RestoreQualificationError(
                "LEDGER_UNVERIFIED",
                "Restore deletion ledger could not be verified",
            ) from error
        if any(event.restore_key_version != self._restore_key_version for event in events):
            raise RestoreQualificationError(
                "RESTORE_KEY_VERSION_UNAVAILABLE",
                "Restore key version required by the deletion ledger is unavailable",
            )
        evidence_by_id = self._validated_evidence(outcome_evidence, events)
        chains = _chains(events)
        requested: list[str] = []
        aborted: list[str] = []
        holds: list[str] = []
        for deletion_id, chain in chains.items():
            if tuple(event.phase for event in chain) != ("PREPARED",):
                continue
            prepared = chain[0]
            evidence = evidence_by_id.get(deletion_id)
            if evidence is None:
                holds.append(deletion_id)
                continue
            if evidence.outcome == "NOT_COMMITTED":
                try:
                    self._coordinator.prove_noncommit_and_abort(
                        deletion_id=deletion_id,
                        now=now,
                    )
                    aborted.append(deletion_id)
                except (DeletionLedgerError, DeletionServiceError):
                    holds.append(deletion_id)
                continue
            try:
                self._fence_committed_restore(prepared)
                self._ledger.append(
                    **_event_arguments(prepared, phase="REQUESTED", occurred_at=now)
                )
                requested.append(deletion_id)
            except (DeletionLedgerError, RestoreQualificationError):
                holds.append(deletion_id)
        try:
            self._ledger.validate()
            current_events = self._ledger.events()
        except DeletionLedgerError as error:
            raise RestoreQualificationError(
                "LEDGER_UNVERIFIED",
                "Restore ledger changed to an unverifiable state",
            ) from error
        suppressed: list[str] = []
        for deletion_id, chain in _chains(current_events).items():
            if any(event.phase in {"REQUESTED", "COMPLETED"} for event in chain):
                self._suppress(chain[0])
                suppressed.append(deletion_id)
        remaining = {event.deletion_id for event in self._ledger.unresolved_preparations()}
        holds = sorted(set(holds).union(remaining))
        self._verify_suppressive_scopes_absent(current_events)
        retention_expired_objects = ImportService(
            self._session_factory,
            self._objects,
        ).expire_raw_objects(now=now)
        suppressed_deletions = tuple(sorted(set(suppressed)))
        requested_deletions = tuple(sorted(set(requested)))
        aborted_deletions = tuple(sorted(set(aborted)))
        quarantine_holds = tuple(holds)
        payload = {
            "schema_version": "restore-qualification-v1",
            "exposure_allowed": not holds,
            "suppressed_deletions": suppressed_deletions,
            "requested_deletions": requested_deletions,
            "aborted_deletions": aborted_deletions,
            "quarantine_holds": quarantine_holds,
            "retention_expired_objects": retention_expired_objects,
            "qualified_at": now.isoformat(),
        }
        digest = _artifact_digest(payload)
        return RestoreQualification(
            schema_version="restore-qualification-v1",
            exposure_allowed=not holds,
            suppressed_deletions=suppressed_deletions,
            requested_deletions=requested_deletions,
            aborted_deletions=aborted_deletions,
            quarantine_holds=quarantine_holds,
            retention_expired_objects=retention_expired_objects,
            qualified_at=now.isoformat(),
            artifact_sha256=digest,
        )

    def _validated_evidence(
        self,
        evidence_items: tuple[TransactionOutcomeEvidence, ...],
        events: tuple[LedgerEvent, ...],
    ) -> dict[str, TransactionOutcomeEvidence]:
        prepared = {event.deletion_id: event for event in events if event.phase == "PREPARED"}
        validated: dict[str, TransactionOutcomeEvidence] = {}
        for evidence in evidence_items:
            event = prepared.get(evidence.deletion_id)
            if event is None or evidence.deletion_id in validated:
                raise RestoreQualificationError(
                    "OUTCOME_EVIDENCE_INVALID",
                    "Transaction outcome evidence does not identify one preparation",
                )
            expected = hmac.new(
                self._outcome_key,
                b"RateReplay.TransactionOutcomeEvidence.v1\x00"
                + evidence.canonical_without_receipt(),
                hashlib.sha256,
            ).hexdigest()
            try:
                observed_at = datetime.fromisoformat(evidence.observed_at)
                prepared_at = datetime.fromisoformat(event.occurred_at)
            except ValueError as error:
                raise RestoreQualificationError(
                    "OUTCOME_EVIDENCE_INVALID",
                    "Transaction outcome evidence timestamp is invalid",
                ) from error
            if not (
                evidence.authority
                and hmac.compare_digest(evidence.prepared_receipt, event.receipt)
                and hmac.compare_digest(evidence.receipt, expected)
                and _aware(observed_at) >= _aware(prepared_at)
            ):
                raise RestoreQualificationError(
                    "OUTCOME_EVIDENCE_INVALID",
                    "Transaction outcome evidence failed integrity or freshness validation",
                )
            validated[evidence.deletion_id] = evidence
        return validated

    def _fence_committed_restore(self, prepared: LedgerEvent) -> None:
        restored = self._target_for_event(prepared)
        if restored is None:
            return
        with self._session_factory.begin() as database:
            target = _target_by_scope(
                database,
                target_kind=restored.kind,
                target_scope_id=restored.scope_id,
                lock=True,
            )
            if target is None:
                return
            user = database.get(UserRecord, restored.owner_user_id)
            control = database.get(DeletionControlOperationRecord, prepared.deletion_id)
            if control is not None and not (
                control.target_kind == restored.kind
                and control.target_scope_id == restored.scope_id
                and control.scope_token == prepared.scope_token
                and control.restore_key_version == prepared.restore_key_version
                and control.original_generation == prepared.original_generation
                and control.deletion_generation == prepared.proposed_generation
                and control.preparation_digest == prepared.preparation_digest
                and control.intent_proof_digest == prepared.intent_proof_digest
            ):
                raise RestoreQualificationError(
                    "COMMITTED_CONTROL_MISMATCH",
                    "Restored deletion control does not match the committed preparation",
                )
            if (
                target.lifecycle_state == "ACTIVE"
                and target.lifecycle_generation == prepared.original_generation
                and (
                    restored.kind == "ACCOUNT"
                    or (user is not None and user.lifecycle_state == "ACTIVE")
                )
            ):
                target.lifecycle_state = "DELETION_PENDING_LEDGER"
                target.lifecycle_generation = prepared.proposed_generation
                return
            if (
                target.lifecycle_state in {"DELETION_PENDING_LEDGER", "DELETING"}
                and target.lifecycle_generation == prepared.proposed_generation
            ):
                return
            raise RestoreQualificationError(
                "COMMITTED_FENCE_MISMATCH",
                "Committed preparation does not match the restored lifecycle generation",
            )

    def _suppress(self, prepared: LedgerEvent) -> None:
        restored = self._target_for_event(prepared)
        if restored is None:
            return
        if restored.kind == "ACCOUNT":
            object_keys = self._objects.list_prefix(f"owners/{restored.owner_user_id}")
        else:
            with self._session_factory() as database:
                control = _restored_resource_control(database, restored, prepared)
                graph = _resource_graph(database, control, deletion_job_id="")
            object_keys = _listed_resource_objects(self._objects, graph)
        for key in object_keys:
            self._objects.delete(key)
        with self._session_factory.begin() as database:
            target = _target_by_scope(
                database,
                target_kind=restored.kind,
                target_scope_id=restored.scope_id,
                lock=True,
            )
            if target is None:
                return
            stored_controls = _restored_controls_for_target(database, restored, target)
            for stored_control in stored_controls:
                database.execute(
                    delete(DeletionFenceTargetRecord).where(
                        DeletionFenceTargetRecord.deletion_id == stored_control.deletion_id
                    )
                )
                database.execute(
                    delete(DeletionLedgerReceiptRecord).where(
                        DeletionLedgerReceiptRecord.deletion_id == stored_control.deletion_id
                    )
                )
                database.execute(
                    delete(DeletionReceiptRecord).where(
                        DeletionReceiptRecord.deletion_id == stored_control.deletion_id
                    )
                )
                database.delete(stored_control)
            database.flush()
            if restored.kind == "ACCOUNT":
                _sweep_owner_rows(
                    database,
                    owner_user_id=restored.owner_user_id,
                    deletion_job_id="",
                )
            else:
                resource_control = _restored_resource_control(database, restored, prepared)
                _delete_resource_rows(database, resource_control, deletion_job_id="")
            database.execute(
                delete(DeletionAuditRecord).where(
                    DeletionAuditRecord.scope_token == prepared.scope_token
                )
            )
            database.delete(target)

    def _target_for_event(self, event: LedgerEvent) -> RestoreTarget | None:
        matches: list[RestoreTarget] = []
        with self._session_factory() as database:
            account_scopes = database.execute(
                select(UserRecord.id, UserRecord.deletion_scope_id)
            ).all()
            import_scopes = database.execute(
                select(
                    ImportRecord.id,
                    ImportRecord.owner_user_id,
                    ImportRecord.deletion_scope_id,
                )
            ).all()
            profile_scopes = database.execute(
                select(
                    ProfileVersionRecord.id,
                    ProfileVersionRecord.owner_user_id,
                    ProfileVersionRecord.deletion_scope_id,
                )
            ).all()
        scopes = (
            tuple(
                RestoreTarget("ACCOUNT", owner_id, owner_id, scope_id)
                for owner_id, scope_id in account_scopes
            )
            + tuple(
                RestoreTarget("IMPORT", resource_id, owner_id, scope_id)
                for resource_id, owner_id, scope_id in import_scopes
            )
            + tuple(
                RestoreTarget("PROFILE", resource_id, owner_id, scope_id)
                for resource_id, owner_id, scope_id in profile_scopes
            )
        )
        for restored in scopes:
            scope_id = restored.scope_id
            candidate = _scope_token(self._restore_key, scope_id)
            if hmac.compare_digest(candidate, event.scope_token):
                matches.append(restored)
        if len(matches) > 1:
            raise RestoreQualificationError(
                "RESTORE_SCOPE_COLLISION",
                "Deletion ledger scope matched more than one restored target",
            )
        return matches[0] if matches else None

    def _verify_suppressive_scopes_absent(self, events: tuple[LedgerEvent, ...]) -> None:
        for chain in _chains(events).values():
            if any(event.phase in {"REQUESTED", "COMPLETED"} for event in chain):
                target = self._target_for_event(chain[0])
                if target is not None:
                    raise RestoreQualificationError(
                        "SUPPRESSIVE_SCOPE_REMAINS",
                        "A suppressive deletion scope remains in the restored database",
                    )


def _restored_resource_control(
    database: Session,
    restored: RestoreTarget,
    prepared: LedgerEvent,
) -> DeletionControlOperationRecord:
    existing = database.scalar(
        select(DeletionControlOperationRecord).where(
            DeletionControlOperationRecord.target_scope_id == restored.scope_id
        )
    )
    if existing is not None:
        if existing.target_kind != restored.kind:
            raise RestoreQualificationError(
                "RESTORE_SCOPE_KIND_MISMATCH",
                "Restored deletion control kind does not match its target",
            )
        return existing
    occurred_at = datetime.fromisoformat(prepared.occurred_at)
    return DeletionControlOperationRecord(
        deletion_id=prepared.deletion_id,
        target_kind=restored.kind,
        target_scope_id=restored.scope_id,
        scope_token=prepared.scope_token,
        restore_key_version=prepared.restore_key_version,
        original_generation=prepared.original_generation,
        deletion_generation=prepared.proposed_generation,
        preparation_digest=prepared.preparation_digest,
        intent_proof_digest=prepared.intent_proof_digest,
        phase="SWEEP",
        artifact_counts_json="{}",
        created_at=occurred_at,
        updated_at=occurred_at,
    )


def _restored_controls_for_target(
    database: Session,
    restored: RestoreTarget,
    target: UserRecord | ImportRecord | ProfileVersionRecord,
) -> tuple[DeletionControlOperationRecord, ...]:
    scope_ids = [restored.scope_id]
    if restored.kind == "ACCOUNT":
        deletion_ids = tuple(
            database.scalars(
                select(DeletionIntentRecord.deletion_id).where(
                    DeletionIntentRecord.owner_user_id == restored.owner_user_id
                )
            )
        )
        return (
            tuple(
                database.scalars(
                    select(DeletionControlOperationRecord).where(
                        DeletionControlOperationRecord.deletion_id.in_(deletion_ids)
                    )
                )
            )
            if deletion_ids
            else ()
        )
    if restored.kind == "IMPORT" and isinstance(target, ImportRecord):
        scope_ids.extend(
            database.scalars(
                select(ProfileVersionRecord.deletion_scope_id).where(
                    ProfileVersionRecord.import_id == target.id,
                    ProfileVersionRecord.owner_user_id == restored.owner_user_id,
                )
            )
        )
    return tuple(
        database.scalars(
            select(DeletionControlOperationRecord).where(
                DeletionControlOperationRecord.target_scope_id.in_(tuple(scope_ids))
            )
        )
    )


def sign_transaction_outcome(
    *,
    deletion_id: str,
    prepared_receipt: str,
    outcome: Literal["COMMITTED", "NOT_COMMITTED"],
    observed_at: datetime,
    authority: str,
    key: bytes,
) -> TransactionOutcomeEvidence:
    if len(key) < 32 or not authority or observed_at.tzinfo is None:
        raise ValueError("Transaction outcome signing inputs are invalid")
    unsigned = TransactionOutcomeEvidence(
        schema_version="transaction-outcome-evidence-v1",
        deletion_id=deletion_id,
        prepared_receipt=prepared_receipt,
        outcome=outcome,
        observed_at=observed_at.astimezone(UTC).isoformat(),
        authority=authority,
        receipt="",
    )
    receipt = hmac.new(
        key,
        b"RateReplay.TransactionOutcomeEvidence.v1\x00" + unsigned.canonical_without_receipt(),
        hashlib.sha256,
    ).hexdigest()
    return TransactionOutcomeEvidence(**{**asdict(unsigned), "receipt": receipt})


def verify_restore_qualification_artifact(
    payload: dict[str, object],
) -> RestoreQualification:
    """Verify and parse the complete exposure-gate artifact."""

    required = {
        "schema_version",
        "exposure_allowed",
        "suppressed_deletions",
        "requested_deletions",
        "aborted_deletions",
        "quarantine_holds",
        "retention_expired_objects",
        "qualified_at",
        "artifact_sha256",
    }
    if set(payload) != required or payload.get("schema_version") != "restore-qualification-v1":
        raise RestoreQualificationError(
            "QUALIFICATION_ARTIFACT_INVALID",
            "Restore qualification artifact schema is invalid",
        )
    unsigned = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    digest = payload["artifact_sha256"]
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest,
        _artifact_digest(unsigned),
    ):
        raise RestoreQualificationError(
            "QUALIFICATION_ARTIFACT_INVALID",
            "Restore qualification artifact digest is invalid",
        )
    try:
        exposure_allowed = payload["exposure_allowed"]
        retention_expired_objects = payload["retention_expired_objects"]
        qualified_at = payload["qualified_at"]
        if (
            not isinstance(exposure_allowed, bool)
            or not isinstance(retention_expired_objects, int)
            or isinstance(retention_expired_objects, bool)
            or retention_expired_objects < 0
            or not isinstance(qualified_at, str)
        ):
            raise TypeError
        parsed_lists = {
            key: _string_tuple(payload[key])
            for key in (
                "suppressed_deletions",
                "requested_deletions",
                "aborted_deletions",
                "quarantine_holds",
            )
        }
        _aware(datetime.fromisoformat(qualified_at))
    except (TypeError, ValueError) as error:
        raise RestoreQualificationError(
            "QUALIFICATION_ARTIFACT_INVALID",
            "Restore qualification artifact values are invalid",
        ) from error
    return RestoreQualification(
        schema_version="restore-qualification-v1",
        exposure_allowed=exposure_allowed,
        suppressed_deletions=parsed_lists["suppressed_deletions"],
        requested_deletions=parsed_lists["requested_deletions"],
        aborted_deletions=parsed_lists["aborted_deletions"],
        quarantine_holds=parsed_lists["quarantine_holds"],
        retention_expired_objects=retention_expired_objects,
        qualified_at=qualified_at,
        artifact_sha256=digest,
    )


def write_restore_qualification_artifact(
    path: Path,
    qualification: RestoreQualification,
) -> None:
    """Atomically persist a private qualification artifact after self-verification."""

    verify_restore_qualification_artifact(json.loads(qualification.artifact_json()))
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, qualification.artifact_json().encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _chains(events: tuple[LedgerEvent, ...]) -> dict[str, tuple[LedgerEvent, ...]]:
    grouped: dict[str, list[LedgerEvent]] = {}
    for event in events:
        grouped.setdefault(event.deletion_id, []).append(event)
    return {deletion_id: tuple(chain) for deletion_id, chain in grouped.items()}


def _artifact_digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError
    if value != sorted(set(value)):
        raise ValueError
    return tuple(value)


def _outcome(value: str) -> Literal["COMMITTED", "NOT_COMMITTED"]:
    if value == "COMMITTED":
        return "COMMITTED"
    if value == "NOT_COMMITTED":
        return "NOT_COMMITTED"
    raise RestoreQualificationError(
        "OUTCOME_EVIDENCE_INVALID",
        "Transaction outcome must be COMMITTED or NOT_COMMITTED",
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
