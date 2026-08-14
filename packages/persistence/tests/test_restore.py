from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger, LedgerEvent
from ratereplay_persistence.deletions import DeletionCoordinator, _event_arguments, _scope_token
from ratereplay_persistence.models import (
    DeletionControlOperationRecord,
    DeletionIntentRecord,
    ImportRecord,
    RawObjectRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.restore import (
    RestoreQualificationError,
    RestoreReconciler,
    TransactionOutcomeEvidence,
    sign_transaction_outcome,
    verify_restore_qualification_artifact,
)
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
OWNER_ID = "1" * 32
SCOPE_ID = "2" * 32
RESTORE_KEY = b"r" * 32
OUTCOME_KEY = b"o" * 32


@dataclass(frozen=True, slots=True)
class Harness:
    sessions: sessionmaker[Session]
    objects: FilesystemObjectStore
    ledger: FilesystemDeletionLedger
    restore: RestoreReconciler


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(tmp_path / "objects")
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=OWNER_ID,
                username_canonical="restore-owner",
                password_hash="test-only",
                deletion_scope_id=SCOPE_ID,
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
    objects.put_file(
        f"owners/{OWNER_ID}/exports/report.json",
        BytesIO(b"private"),
        maximum_bytes=1024,
    )
    return Harness(
        sessions=sessions,
        objects=objects,
        ledger=ledger,
        restore=RestoreReconciler(
            sessions,
            objects,
            ledger,
            restore_key=RESTORE_KEY,
            restore_key_version="restore-v1",
            outcome_evidence_key=OUTCOME_KEY,
        ),
    )


def _prepare(harness: Harness, *, deletion_id: str = "3" * 32) -> LedgerEvent:
    return harness.ledger.append(
        deletion_id=deletion_id,
        phase="PREPARED",
        scope_token=_scope_token(RESTORE_KEY, SCOPE_ID),
        restore_key_version="restore-v1",
        original_generation=0,
        proposed_generation=1,
        preparation_digest="4" * 64,
        intent_proof_digest="5" * 64,
        occurred_at=NOW + timedelta(seconds=1),
    )


def _request(harness: Harness, prepared: LedgerEvent) -> None:
    harness.ledger.append(
        **_event_arguments(
            prepared,
            phase="REQUESTED",
            occurred_at=NOW + timedelta(seconds=2),
        )
    )


def _evidence(
    prepared: LedgerEvent,
    outcome: str,
    *,
    key: bytes = OUTCOME_KEY,
    observed_at: datetime = NOW + timedelta(seconds=3),
) -> TransactionOutcomeEvidence:
    assert outcome in {"COMMITTED", "NOT_COMMITTED"}
    return sign_transaction_outcome(
        deletion_id=prepared.deletion_id,
        prepared_receipt=prepared.receipt,
        outcome=outcome,  # type: ignore[arg-type]
        observed_at=observed_at,
        authority="postgres-wal-qualification",
        key=key,
    )


def test_requested_event_suppresses_predeletion_backup_before_exposure(
    harness: Harness,
) -> None:
    prepared = _prepare(harness)
    _request(harness, prepared)

    qualification = harness.restore.qualify(now=NOW + timedelta(days=90))

    assert qualification.exposure_allowed
    assert qualification.suppressed_deletions == (prepared.deletion_id,)
    assert not harness.objects.exists(f"owners/{OWNER_ID}/exports/report.json")
    with harness.sessions() as database:
        assert database.get(UserRecord, OWNER_ID) is None


def test_account_restore_suppression_removes_child_deletion_controls(
    harness: Harness,
) -> None:
    child_deletion_id = "a" * 32
    child_scope_id = "b" * 32
    with harness.sessions.begin() as database:
        database.add(
            ImportRecord(
                id="c" * 32,
                owner_user_id=OWNER_ID,
                state="READY",
                lifecycle_state="DELETING",
                lifecycle_generation=1,
                deletion_scope_id=child_scope_id,
                adapter="ESPI_XML",
                raw_content_hash="d" * 64,
                created_at=NOW,
            )
        )
        database.add(
            DeletionIntentRecord(
                deletion_id=child_deletion_id,
                owner_user_id=OWNER_ID,
                idempotency_key="child-delete",
                request_schema_version="deletion-intent-v1",
                canonical_payload_hash="e" * 64,
                receipt_digest="f" * 64,
                target_kind="IMPORT",
                target_scope_id=child_scope_id,
                original_generation=0,
                proposed_generation=1,
                state="CONSUMED",
                preparation_digest="0" * 64,
                preparation_receipt="1" * 64,
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
                prepared_at=NOW + timedelta(seconds=1),
                consumed_at=NOW + timedelta(seconds=2),
            )
        )
        database.add(
            DeletionControlOperationRecord(
                deletion_id=child_deletion_id,
                target_kind="IMPORT",
                target_scope_id=child_scope_id,
                scope_token=_scope_token(RESTORE_KEY, child_scope_id),
                restore_key_version="restore-v1",
                original_generation=0,
                deletion_generation=1,
                preparation_digest="0" * 64,
                intent_proof_digest="2" * 64,
                phase="REQUESTED",
                artifact_counts_json="{}",
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=2),
            )
        )
    prepared = _prepare(harness)
    _request(harness, prepared)

    qualification = harness.restore.qualify(now=NOW + timedelta(seconds=3))

    assert qualification.exposure_allowed
    with harness.sessions() as database:
        assert database.get(UserRecord, OWNER_ID) is None
        assert database.get(DeletionControlOperationRecord, child_deletion_id) is None


def test_requested_import_deletion_suppresses_only_restored_child_scope(
    harness: Harness,
) -> None:
    import_id = "a" * 32
    import_scope_id = "b" * 32
    raw_key = f"owners/{OWNER_ID}/imports/{import_id}/raw"
    harness.objects.put_file(raw_key, BytesIO(b"private child raw"), maximum_bytes=1024)
    with harness.sessions.begin() as database:
        database.add(
            ImportRecord(
                id=import_id,
                owner_user_id=OWNER_ID,
                state="READY",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                deletion_scope_id=import_scope_id,
                adapter="ESPI_XML",
                raw_content_hash="c" * 64,
                created_at=NOW,
            )
        )
        database.add(
            RawObjectRecord(
                id="d" * 32,
                owner_user_id=OWNER_ID,
                import_id=import_id,
                object_key=raw_key,
                content_hash="c" * 64,
                size_bytes=17,
                state="AVAILABLE",
                created_at=NOW,
                expires_at=NOW + timedelta(days=1),
            )
        )
    prepared = harness.ledger.append(
        deletion_id="e" * 32,
        phase="PREPARED",
        scope_token=_scope_token(RESTORE_KEY, import_scope_id),
        restore_key_version="restore-v1",
        original_generation=0,
        proposed_generation=1,
        preparation_digest="f" * 64,
        intent_proof_digest="0" * 64,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _request(harness, prepared)

    qualification = harness.restore.qualify(now=NOW + timedelta(seconds=3))

    assert qualification.exposure_allowed
    assert qualification.suppressed_deletions == (prepared.deletion_id,)
    assert not harness.objects.exists(raw_key)
    with harness.sessions() as database:
        assert database.get(UserRecord, OWNER_ID) is not None
        assert database.get(ImportRecord, import_id) is None


def test_unresolved_preparation_holds_restore_indefinitely(harness: Harness) -> None:
    prepared = _prepare(harness)

    qualification = harness.restore.qualify(now=NOW + timedelta(days=3650))

    assert not qualification.exposure_allowed
    assert qualification.quarantine_holds == (prepared.deletion_id,)
    assert harness.objects.exists(f"owners/{OWNER_ID}/exports/report.json")
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None and user.lifecycle_state == "ACTIVE"


def test_positive_commit_evidence_requests_and_suppresses_old_active_backup(
    harness: Harness,
) -> None:
    prepared = _prepare(harness)

    qualification = harness.restore.qualify(
        now=NOW + timedelta(seconds=4),
        outcome_evidence=(_evidence(prepared, "COMMITTED"),),
    )

    assert qualification.exposure_allowed
    assert qualification.requested_deletions == (prepared.deletion_id,)
    assert tuple(event.phase for event in harness.ledger.chain(prepared.deletion_id)) == (
        "PREPARED",
        "REQUESTED",
    )
    with harness.sessions() as database:
        assert database.get(UserRecord, OWNER_ID) is None


def test_positive_noncommit_evidence_uses_strict_abort_proof(harness: Harness) -> None:
    coordinator = DeletionCoordinator(
        harness.sessions,
        harness.ledger,
        restore_key=RESTORE_KEY,
    )
    intent = coordinator.create_intent(
        owner_user_id=OWNER_ID,
        idempotency_key="restore-noncommit",
        receipt_secret=b"s" * 32,
        now=NOW,
    )
    authorized = coordinator._authorized_intent(
        owner_user_id=OWNER_ID,
        deletion_id=intent.deletion_id,
        receipt_secret=b"s" * 32,
        now=NOW,
    )
    prepared = coordinator._ensure_prepared(
        authorized,
        now=NOW + timedelta(seconds=1),
    )

    qualification = harness.restore.qualify(
        now=NOW + timedelta(seconds=4),
        outcome_evidence=(_evidence(prepared, "NOT_COMMITTED"),),
    )

    assert qualification.exposure_allowed
    assert qualification.aborted_deletions == (prepared.deletion_id,)
    assert tuple(event.phase for event in harness.ledger.chain(prepared.deletion_id)) == (
        "PREPARED",
        "ABORTED",
    )
    with harness.sessions() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None and user.lifecycle_state == "ACTIVE"


@pytest.mark.parametrize("failure", ["wrong-key", "stale"])
def test_invalid_outcome_evidence_fails_closed(harness: Harness, failure: str) -> None:
    prepared = _prepare(harness)
    evidence = (
        _evidence(prepared, "COMMITTED", key=b"w" * 32)
        if failure == "wrong-key"
        else _evidence(prepared, "COMMITTED", observed_at=NOW)
    )

    with pytest.raises(RestoreQualificationError) as raised:
        harness.restore.qualify(
            now=NOW + timedelta(seconds=4),
            outcome_evidence=(evidence,),
        )

    assert raised.value.code == "OUTCOME_EVIDENCE_INVALID"


def test_outcome_evidence_rejects_unknown_schema() -> None:
    with pytest.raises(RestoreQualificationError) as raised:
        TransactionOutcomeEvidence.from_dict(
            {
                "schema_version": "transaction-outcome-evidence-v2",
                "deletion_id": "3" * 32,
                "prepared_receipt": "4" * 64,
                "outcome": "COMMITTED",
                "observed_at": NOW.isoformat(),
                "authority": "test",
                "receipt": "5" * 64,
            }
        )
    assert raised.value.code == "OUTCOME_EVIDENCE_INVALID"


def test_unverifiable_ledger_and_missing_restore_key_version_fail_closed(
    harness: Harness,
    tmp_path: Path,
) -> None:
    _prepare(harness)
    ledger_path = tmp_path / "ledger" / "deletion-ledger-v2.jsonl"
    records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    ciphertext = bytearray(base64.b64decode(records[0]["ciphertext"]))
    ciphertext[0] ^= 1
    records[0]["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    ledger_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(RestoreQualificationError) as tampered:
        harness.restore.qualify(now=NOW + timedelta(seconds=2))
    assert tampered.value.code == "LEDGER_UNVERIFIED"

    other_ledger = FilesystemDeletionLedger(
        tmp_path / "other-ledger",
        integrity_key=b"l" * 32,
    )
    other_ledger.append(
        deletion_id="6" * 32,
        phase="PREPARED",
        scope_token=_scope_token(RESTORE_KEY, SCOPE_ID),
        restore_key_version="retired-key-v0",
        original_generation=0,
        proposed_generation=1,
        preparation_digest="7" * 64,
        intent_proof_digest="8" * 64,
        occurred_at=NOW,
    )
    reconciler = RestoreReconciler(
        harness.sessions,
        harness.objects,
        other_ledger,
        restore_key=RESTORE_KEY,
        restore_key_version="restore-v1",
        outcome_evidence_key=OUTCOME_KEY,
    )
    with pytest.raises(RestoreQualificationError) as missing_key:
        reconciler.qualify(now=NOW)
    assert missing_key.value.code == "RESTORE_KEY_VERSION_UNAVAILABLE"


def test_qualification_artifact_is_verified_deterministic_and_redacted(
    harness: Harness,
) -> None:
    prepared = _prepare(harness)
    _request(harness, prepared)
    first = harness.restore.qualify(now=NOW + timedelta(seconds=4))
    second = harness.restore.qualify(now=NOW + timedelta(seconds=4))

    assert first.artifact_json() == second.artifact_json()
    payload = json.loads(first.artifact_json())
    assert verify_restore_qualification_artifact(payload) == first
    assert "restore-owner" not in first.artifact_json()
    assert SCOPE_ID not in first.artifact_json()
    assert f"owners/{OWNER_ID}" not in first.artifact_json()
    payload["exposure_allowed"] = False
    with pytest.raises(RestoreQualificationError) as raised:
        verify_restore_qualification_artifact(payload)
    assert raised.value.code == "QUALIFICATION_ARTIFACT_INVALID"


def test_restore_reruns_expired_raw_object_retention(harness: Harness) -> None:
    raw_key = f"owners/{OWNER_ID}/raw/expired.xml"
    harness.objects.put_file(raw_key, BytesIO(b"raw"), maximum_bytes=1024)
    with harness.sessions.begin() as database:
        database.add(
            ImportRecord(
                id="7" * 32,
                owner_user_id=OWNER_ID,
                state="READY",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                adapter="ESPI_XML",
                raw_content_hash="8" * 64,
                created_at=NOW - timedelta(days=2),
            )
        )
        database.add(
            RawObjectRecord(
                id="9" * 32,
                owner_user_id=OWNER_ID,
                import_id="7" * 32,
                object_key=raw_key,
                content_hash="8" * 64,
                size_bytes=3,
                state="AVAILABLE",
                created_at=NOW - timedelta(days=2),
                expires_at=NOW - timedelta(days=1),
            )
        )

    qualification = harness.restore.qualify(now=NOW)

    assert qualification.exposure_allowed
    assert qualification.retention_expired_objects == 1
    assert not harness.objects.exists(raw_key)
    with harness.sessions() as database:
        raw = database.get(RawObjectRecord, "9" * 32)
        assert raw is not None and raw.state == "DELETED"
