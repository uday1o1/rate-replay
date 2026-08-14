"""Typed, content-hashed, append-only audit events."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ratereplay_persistence.models import AuditEventRecord

AUDIT_SCHEMA_VERSION: Final = "ratereplay-audit-event-v1"
AUDIT_SUBJECT_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
AuditEventType = Literal[
    "AUTH_REGISTERED",
    "AUTH_LOGIN_SUCCEEDED",
    "AUTH_LOGOUT",
    "IMPORT_SUBMITTED",
    "IMPORT_CONFIRMED",
    "PROFILE_INSTALLED",
    "JOB_SUBMITTED",
    "JOB_LEASED",
    "JOB_RETRY_SCHEDULED",
    "JOB_SUCCEEDED",
    "JOB_FAILED",
    "JOB_CANCELLED",
]
AuditSubjectType = Literal["ACCOUNT", "IMPORT", "JOB", "PROFILE", "SESSION"]
AuditOutcome = Literal[
    "ACCEPTED",
    "CANCELLED",
    "FAILED",
    "LEASED",
    "RETRY_SCHEDULED",
    "SUCCEEDED",
]


class AuditEventError(RuntimeError):
    pass


def append_audit_event(
    database: Session,
    *,
    owner_user_id: str | None,
    event_type: AuditEventType,
    subject_type: AuditSubjectType,
    subject_id: str,
    sequence: int,
    outcome: AuditOutcome,
    now: datetime,
) -> AuditEventRecord:
    """Append one idempotent event inside the caller's transaction."""

    if owner_user_id is not None and len(owner_user_id) != 32:
        raise AuditEventError("Audit owner identifier is invalid")
    if AUDIT_SUBJECT_PATTERN.fullmatch(subject_id) is None:
        raise AuditEventError("Audit subject identifier is invalid")
    if sequence < 0:
        raise AuditEventError("Audit event sequence cannot be negative")
    existing = database.scalar(
        select(AuditEventRecord).where(
            AuditEventRecord.owner_user_id == owner_user_id,
            AuditEventRecord.event_type == event_type,
            AuditEventRecord.subject_type == subject_type,
            AuditEventRecord.subject_id == subject_id,
            AuditEventRecord.sequence == sequence,
        )
    )
    if existing is not None:
        if existing.outcome != outcome or not verify_audit_event(existing):
            raise AuditEventError("Existing audit transition conflicts with the requested event")
        return existing
    event_id = secrets.token_hex(16)
    recorded_at = _aware(now)
    event_hash = _event_hash(
        event_id=event_id,
        owner_user_id=owner_user_id,
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        sequence=sequence,
        outcome=outcome,
        recorded_at=recorded_at,
    )
    event = AuditEventRecord(
        id=event_id,
        owner_user_id=owner_user_id,
        schema_version=AUDIT_SCHEMA_VERSION,
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        sequence=sequence,
        outcome=outcome,
        recorded_at=recorded_at,
        event_hash=event_hash,
    )
    database.add(event)
    return event


def verify_audit_event(event: AuditEventRecord) -> bool:
    if event.schema_version != AUDIT_SCHEMA_VERSION:
        return False
    expected = _event_hash(
        event_id=event.id,
        owner_user_id=event.owner_user_id,
        event_type=event.event_type,
        subject_type=event.subject_type,
        subject_id=event.subject_id,
        sequence=event.sequence,
        outcome=event.outcome,
        recorded_at=_aware(event.recorded_at),
    )
    return secrets.compare_digest(event.event_hash, expected)


def _event_hash(
    *,
    event_id: str,
    owner_user_id: str | None,
    event_type: str,
    subject_type: str,
    subject_id: str,
    sequence: int,
    outcome: str,
    recorded_at: datetime,
) -> str:
    canonical = json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "outcome": outcome,
            "owner_user_id": owner_user_id,
            "recorded_at": recorded_at.isoformat(),
            "schema_version": AUDIT_SCHEMA_VERSION,
            "sequence": sequence,
            "subject_id": subject_id,
            "subject_type": subject_type,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(b"RateReplay.AuditEvent.v1\x00" + canonical).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
