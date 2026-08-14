from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.models import (
    DeletionAuditRecord,
    DeletionControlOperationRecord,
    DeletionIntentRecord,
    DeletionLedgerReceiptRecord,
    DeletionReceiptRecord,
    UserRecord,
)
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 8, 14, tzinfo=UTC)


def test_control_plane_records_exclude_direct_owner_identity() -> None:
    forbidden = {"owner_user_id", "username_canonical", "profile_hash", "utility_id"}
    for model in (
        DeletionReceiptRecord,
        DeletionControlOperationRecord,
        DeletionLedgerReceiptRecord,
        DeletionAuditRecord,
    ):
        assert forbidden.isdisjoint(model.__table__.columns.keys())


def test_audit_tombstone_has_only_permitted_deletion_metadata() -> None:
    assert set(DeletionAuditRecord.__table__.columns.keys()) == {
        "deletion_id",
        "receipt_verifier",
        "verifier_expires_at",
        "scope_token",
        "restore_key_version",
        "deletion_generation",
        "completed_at",
        "artifact_counts_json",
        "status",
        "status_code",
    }


def test_database_enforces_one_intent_and_one_control_scope_per_target() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id="1" * 32,
                username_canonical="deletion-owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=3,
                deletion_scope_id=None,
            )
        )
        database.add(
            DeletionIntentRecord(
                deletion_id="2" * 32,
                owner_user_id="1" * 32,
                idempotency_key="intent-key-1",
                request_schema_version="deletion-intent-v1",
                canonical_payload_hash="a" * 64,
                receipt_digest="b" * 64,
                original_generation=3,
                proposed_generation=4,
                state="INTENT_CREATED",
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
            )
        )

    with pytest.raises(IntegrityError), sessions.begin() as database:
        database.add(
            DeletionIntentRecord(
                deletion_id="3" * 32,
                owner_user_id="1" * 32,
                idempotency_key="intent-key-2",
                request_schema_version="deletion-intent-v1",
                canonical_payload_hash="c" * 64,
                receipt_digest="d" * 64,
                original_generation=3,
                proposed_generation=4,
                state="INTENT_CREATED",
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
            )
        )


def test_receipt_and_control_rows_persist_without_owner_foreign_keys() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    deletion_id = "4" * 32
    with sessions.begin() as database:
        database.add(
            DeletionReceiptRecord(
                deletion_id=deletion_id,
                receipt_verifier="$argon2id$test",
                status="DELETION_PENDING_LEDGER",
                artifact_counts_json="{}",
                created_at=NOW,
            )
        )
        database.add(
            DeletionControlOperationRecord(
                deletion_id=deletion_id,
                target_scope_id="5" * 32,
                scope_token="e" * 64,
                restore_key_version="restore-v1",
                original_generation=0,
                deletion_generation=1,
                preparation_digest="f" * 64,
                phase="FENCE",
                artifact_counts_json="{}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        database.add(
            DeletionLedgerReceiptRecord(
                id="6" * 32,
                deletion_id=deletion_id,
                phase="PREPARED",
                canonical_digest="7" * 64,
                integrity_receipt="8" * 64,
                acknowledged_at=NOW,
            )
        )

    with sessions() as database:
        assert database.get(DeletionReceiptRecord, deletion_id) is not None
        assert database.get(DeletionControlOperationRecord, deletion_id) is not None


def test_invalid_user_lifecycle_is_rejected_by_database() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with pytest.raises(IntegrityError), sessions.begin() as database:
        database.add(
            UserRecord(
                id="9" * 32,
                username_canonical="invalid-owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="RESTORED",
                lifecycle_generation=0,
                deletion_scope_id=None,
            )
        )
