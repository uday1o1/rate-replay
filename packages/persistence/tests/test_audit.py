from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ratereplay_persistence.audit import AuditEventError, append_audit_event, verify_audit_event
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.models import AuditEventRecord, UserRecord
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
OWNER_ID = "1" * 32
SUBJECT_ID = "2" * 32


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as database:
        database.add(
            UserRecord(
                id=OWNER_ID,
                username_canonical="audit_owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
    return factory


def test_audit_event_is_hashed_idempotent_and_has_no_generic_payload(
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as database:
        first = append_audit_event(
            database,
            owner_user_id=OWNER_ID,
            event_type="JOB_SUBMITTED",
            subject_type="JOB",
            subject_id=SUBJECT_ID,
            sequence=0,
            outcome="ACCEPTED",
            now=NOW,
        )
        database.flush()
        repeated = append_audit_event(
            database,
            owner_user_id=OWNER_ID,
            event_type="JOB_SUBMITTED",
            subject_type="JOB",
            subject_id=SUBJECT_ID,
            sequence=0,
            outcome="ACCEPTED",
            now=NOW,
        )
        assert repeated.id == first.id
        assert verify_audit_event(first)
    columns = set(inspect(AuditEventRecord).columns.keys())
    assert columns == {
        "event_hash",
        "event_type",
        "id",
        "outcome",
        "owner_user_id",
        "recorded_at",
        "schema_version",
        "sequence",
        "subject_id",
        "subject_type",
    }


def test_audit_event_rejects_conflict_tampering_update_and_delete(
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as database:
        event = append_audit_event(
            database,
            owner_user_id=OWNER_ID,
            event_type="JOB_FAILED",
            subject_type="JOB",
            subject_id=SUBJECT_ID,
            sequence=1,
            outcome="FAILED",
            now=NOW,
        )
    with sessions.begin() as database, pytest.raises(AuditEventError, match="conflicts"):
        append_audit_event(
            database,
            owner_user_id=OWNER_ID,
            event_type="JOB_FAILED",
            subject_type="JOB",
            subject_id=SUBJECT_ID,
            sequence=1,
            outcome="CANCELLED",
            now=NOW,
        )
    with sessions() as database:
        stored = database.get(AuditEventRecord, event.id)
        assert stored is not None
        stored.outcome = "CANCELLED"
        assert not verify_audit_event(stored)
        with pytest.raises(RuntimeError, match="immutable"):
            database.flush()
        database.rollback()
        stored = database.get(AuditEventRecord, event.id)
        assert stored is not None
        database.delete(stored)
        with pytest.raises(RuntimeError, match="append-only"):
            database.flush()


@pytest.mark.parametrize(
    ("owner_id", "subject_id", "sequence"),
    [
        ("short", SUBJECT_ID, 0),
        (OWNER_ID, "unsafe/id", 0),
        (OWNER_ID, "a" * 65, 0),
        (OWNER_ID, SUBJECT_ID, -1),
    ],
)
def test_audit_event_rejects_invalid_identity(
    owner_id: str,
    subject_id: str,
    sequence: int,
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as database, pytest.raises(AuditEventError):
        append_audit_event(
            database,
            owner_user_id=owner_id,
            event_type="JOB_SUBMITTED",
            subject_type="JOB",
            subject_id=subject_id,
            sequence=sequence,
            outcome="ACCEPTED",
            now=NOW,
        )
