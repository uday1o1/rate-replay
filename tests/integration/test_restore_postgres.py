from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger, LedgerEvent
from ratereplay_persistence.deletions import _event_arguments, _scope_token
from ratereplay_persistence.models import (
    DeletionControlOperationRecord,
    ImportRecord,
    RawObjectRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.restore import (
    RestoreReconciler,
    TransactionOutcomeEvidence,
    sign_transaction_outcome,
)
from sqlalchemy import delete

pytestmark = pytest.mark.postgres

RESTORE_KEY = b"r" * 32
OUTCOME_KEY = b"o" * 32


def test_postgres_restore_drill_reconciles_every_loss_boundary(tmp_path: Path) -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(tmp_path / "objects")
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    restore = RestoreReconciler(
        sessions,
        objects,
        ledger,
        restore_key=RESTORE_KEY,
        restore_key_version="restore-v1",
        outcome_evidence_key=OUTCOME_KEY,
    )
    now = datetime.now(UTC)
    requested_owner = secrets.token_hex(16)
    prepared_owner = secrets.token_hex(16)
    fenced_owner = secrets.token_hex(16)
    retention_owner = secrets.token_hex(16)
    owner_ids = (requested_owner, prepared_owner, fenced_owner, retention_owner)
    scopes = {owner_id: secrets.token_hex(16) for owner_id in owner_ids}
    requested = _append_prepared(ledger, scopes[requested_owner], now)
    ledger.append(
        **_event_arguments(
            requested,
            phase="REQUESTED",
            occurred_at=now + timedelta(seconds=1),
        )
    )
    prepared = _append_prepared(ledger, scopes[prepared_owner], now)
    fenced = _append_prepared(ledger, scopes[fenced_owner], now)
    raw_key = f"owners/{retention_owner}/raw/expired.xml"
    try:
        with sessions.begin() as database:
            for owner_id in owner_ids:
                database.add(
                    UserRecord(
                        id=owner_id,
                        username_canonical=f"restore_{owner_id}",
                        password_hash="test-only",
                        deletion_scope_id=scopes[owner_id],
                        created_at=now - timedelta(days=3),
                        lifecycle_state=(
                            "DELETION_PENDING_LEDGER" if owner_id == fenced_owner else "ACTIVE"
                        ),
                        lifecycle_generation=1 if owner_id == fenced_owner else 0,
                    )
                )
            database.flush()
            database.add(
                DeletionControlOperationRecord(
                    deletion_id=fenced.deletion_id,
                    target_scope_id=scopes[fenced_owner],
                    scope_token=fenced.scope_token,
                    restore_key_version=fenced.restore_key_version,
                    original_generation=fenced.original_generation,
                    deletion_generation=fenced.proposed_generation,
                    preparation_digest=fenced.preparation_digest,
                    intent_proof_digest=fenced.intent_proof_digest,
                    phase="FENCE",
                    artifact_counts_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
            database.add(
                ImportRecord(
                    id=retention_owner,
                    owner_user_id=retention_owner,
                    state="READY",
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    adapter="ESPI_XML",
                    raw_content_hash="a" * 64,
                    created_at=now - timedelta(days=2),
                )
            )
            database.add(
                RawObjectRecord(
                    id=retention_owner,
                    owner_user_id=retention_owner,
                    import_id=retention_owner,
                    object_key=raw_key,
                    content_hash="a" * 64,
                    size_bytes=3,
                    state="AVAILABLE",
                    created_at=now - timedelta(days=2),
                    expires_at=now - timedelta(days=1),
                )
            )
        for owner_id in (requested_owner, prepared_owner, fenced_owner):
            objects.put_file(
                f"owners/{owner_id}/exports/report.json",
                BytesIO(b"private"),
                maximum_bytes=1024,
            )
        objects.put_file(raw_key, BytesIO(b"raw"), maximum_bytes=1024)

        held = restore.qualify(now=now + timedelta(seconds=2))

        assert not held.exposure_allowed
        assert held.quarantine_holds == tuple(sorted((prepared.deletion_id, fenced.deletion_id)))
        assert held.retention_expired_objects == 1
        assert not objects.exists(f"owners/{requested_owner}/exports/report.json")
        assert not objects.exists(raw_key)
        with sessions() as database:
            assert database.get(UserRecord, requested_owner) is None
            assert database.get(UserRecord, prepared_owner) is not None
            assert database.get(UserRecord, fenced_owner) is not None
            raw = database.get(RawObjectRecord, retention_owner)
            assert raw is not None and raw.state == "DELETED"

        qualified = restore.qualify(
            now=now + timedelta(seconds=4),
            outcome_evidence=(
                _commit_evidence(prepared, now + timedelta(seconds=3)),
                _commit_evidence(fenced, now + timedelta(seconds=3)),
            ),
        )

        assert qualified.exposure_allowed
        assert qualified.quarantine_holds == ()
        assert set(qualified.requested_deletions) == {
            prepared.deletion_id,
            fenced.deletion_id,
        }
        for owner_id in (requested_owner, prepared_owner, fenced_owner):
            assert not objects.exists(f"owners/{owner_id}/exports/report.json")
        with sessions() as database:
            assert database.get(UserRecord, prepared_owner) is None
            assert database.get(UserRecord, fenced_owner) is None
            assert database.get(UserRecord, retention_owner) is not None
    finally:
        with sessions.begin() as database:
            database.execute(
                delete(DeletionControlOperationRecord).where(
                    DeletionControlOperationRecord.deletion_id.in_(
                        (requested.deletion_id, prepared.deletion_id, fenced.deletion_id)
                    )
                )
            )
            database.execute(
                delete(RawObjectRecord).where(RawObjectRecord.owner_user_id.in_(owner_ids))
            )
            database.execute(delete(ImportRecord).where(ImportRecord.owner_user_id.in_(owner_ids)))
            database.execute(delete(UserRecord).where(UserRecord.id.in_(owner_ids)))
        engine.dispose()


def _append_prepared(
    ledger: FilesystemDeletionLedger,
    scope_id: str,
    now: datetime,
) -> LedgerEvent:
    return ledger.append(
        deletion_id=secrets.token_hex(16),
        phase="PREPARED",
        scope_token=_scope_token(RESTORE_KEY, scope_id),
        restore_key_version="restore-v1",
        original_generation=0,
        proposed_generation=1,
        preparation_digest=secrets.token_hex(32),
        intent_proof_digest=secrets.token_hex(32),
        occurred_at=now,
    )


def _commit_evidence(
    prepared: LedgerEvent,
    observed_at: datetime,
) -> TransactionOutcomeEvidence:
    return sign_transaction_outcome(
        deletion_id=prepared.deletion_id,
        prepared_receipt=prepared.receipt,
        outcome="COMMITTED",
        observed_at=observed_at,
        authority="postgres-wal-qualification",
        key=OUTCOME_KEY,
    )
