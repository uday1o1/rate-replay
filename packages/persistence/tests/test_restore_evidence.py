from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ratereplay_persistence.backups import MaterializedBackup, MaterializedBackupObject
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.restore import RestoreQualificationError, RestoreReconciler
from ratereplay_persistence.restore_evidence import (
    bind_restore_exposure,
    verify_restore_exposure_artifact,
    write_restore_exposure_artifact,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def test_restore_exposure_artifact_binds_exact_private_instance_without_paths(
    tmp_path: Path,
) -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    ledger_root = tmp_path / "ledger"
    ledger = FilesystemDeletionLedger(ledger_root, integrity_key=b"l" * 32)
    qualification = RestoreReconciler(
        sessions,
        FilesystemObjectStore(tmp_path / "objects"),
        ledger,
        restore_key=b"r" * 32,
        restore_key_version="restore-v1",
        outcome_evidence_key=b"o" * 32,
    ).qualify(now=NOW)
    private_path = tmp_path / "materialized" / "private-object"
    private_path.parent.mkdir()
    private_path.write_bytes(b"private")
    materialized = MaterializedBackup(
        backup_id="20260814T120000000000Z-0123456789abcdef",
        manifest_content_hash="1" * 64,
        database_dump_path=tmp_path / "database.dump",
        database_content_hash="2" * 64,
        objects=(
            MaterializedBackupObject(
                source_key="owners/private-user/private-file",
                path=private_path,
                content_hash="3" * 64,
                size_bytes=7,
            ),
        ),
    )

    artifact = bind_restore_exposure(
        materialized,
        qualification,
        deletion_ledger_root=ledger_root,
        database_revision="0014_restore_controls",
        bound_at=NOW,
    )

    assert artifact.exposure_allowed
    assert verify_restore_exposure_artifact(asdict(artifact)) == artifact
    encoded = artifact.artifact_json()
    assert "private-user" not in encoded
    assert "private-file" not in encoded
    assert str(tmp_path) not in encoded
    destination = tmp_path / "evidence" / "exposure.json"
    write_restore_exposure_artifact(destination, artifact)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert json.loads(destination.read_text(encoding="ascii"))["artifact_sha256"] == (
        artifact.artifact_sha256
    )
    tampered = asdict(artifact)
    tampered["exposure_allowed"] = False
    with pytest.raises(RestoreQualificationError) as invalid:
        verify_restore_exposure_artifact(tampered)
    assert invalid.value.code == "RESTORE_EXPOSURE_ARTIFACT_INVALID"
    engine.dispose()


def test_restore_exposure_binding_requires_ledger_head_and_revision(tmp_path: Path) -> None:
    materialized = MaterializedBackup(
        backup_id="20260814T120000000000Z-0123456789abcdef",
        manifest_content_hash="1" * 64,
        database_dump_path=tmp_path / "database.dump",
        database_content_hash="2" * 64,
        objects=(),
    )
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    ledger = FilesystemDeletionLedger(tmp_path / "ledger", integrity_key=b"l" * 32)
    qualification = RestoreReconciler(
        sessions,
        FilesystemObjectStore(tmp_path / "objects"),
        ledger,
        restore_key=b"r" * 32,
        restore_key_version="restore-v1",
        outcome_evidence_key=b"o" * 32,
    ).qualify(now=NOW)

    with pytest.raises(ValueError, match="revision"):
        bind_restore_exposure(
            materialized,
            qualification,
            deletion_ledger_root=tmp_path / "ledger",
            database_revision="",
            bound_at=NOW,
        )
    with pytest.raises(RestoreQualificationError) as missing_head:
        bind_restore_exposure(
            materialized,
            qualification,
            deletion_ledger_root=tmp_path / "missing-ledger",
            database_revision="0014",
            bound_at=NOW,
        )
    assert missing_head.value.code == "LEDGER_UNVERIFIED"
    engine.dispose()
