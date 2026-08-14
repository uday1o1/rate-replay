import json
import re
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletions import _scope_token
from ratereplay_persistence.models import JobRecord, UserRecord
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_worker.cli import app
from typer.testing import CliRunner


def test_worker_cli_requires_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATEREPLAY_DATABASE_URL", raising=False)
    result = CliRunner().invoke(app, ["run-once"])
    assert result.exit_code == 2
    assert "RATEREPLAY_DATABASE_URL is required" in result.output


def test_worker_cli_exposes_one_shot_and_continuous_modes() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run-once" in result.output
    assert "run" in result.output
    assert "reconcile-deletions-once" in result.output
    assert "reconcile-deletions" in result.output
    assert "run-deletion-once" in result.output
    assert "run-deletions" in result.output
    assert "run-replay-once" in result.output
    assert "run-replays" in result.output
    assert "run-comparison-once" in result.output
    assert "run-comparisons" in result.output
    assert "run-report-once" in result.output
    assert "run-reports" in result.output
    assert "schedule-retention-once" in result.output
    assert "schedule-retention" in result.output
    assert "run-retention-once" in result.output
    assert "run-retention" in result.output
    assert "create-backup" in result.output
    assert "verify-backup" in result.output
    assert "expire-backups-once" in result.output
    assert "qualify-restore" in result.output
    assert "verify-restore-qualification" in result.output


def test_retention_cli_schedules_idempotently_and_runs_system_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "retention.sqlite"
    database_url = f"sqlite+pysqlite:///{database_path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    ledger_key_file = tmp_path / "ledger.key"
    ledger_key_file.write_bytes(b"l" * 32)
    monkeypatch.setenv("RATEREPLAY_DATABASE_URL", database_url)
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("RATEREPLAY_DELETION_LEDGER_ROOT", str(tmp_path / "ledger"))
    monkeypatch.setenv("RATEREPLAY_DELETION_LEDGER_KEY_FILE", str(ledger_key_file))

    runner = CliRunner()
    scheduled = runner.invoke(app, ["schedule-retention-once"])
    repeated = runner.invoke(app, ["schedule-retention-once"])
    processed = runner.invoke(app, ["run-retention-once"])
    idle = runner.invoke(app, ["run-retention-once"])

    assert scheduled.exit_code == 0 and "repeated=false" in scheduled.output
    assert repeated.exit_code == 0 and "repeated=true" in repeated.output
    assert processed.exit_code == 0 and processed.output.strip() == "processed"
    assert idle.exit_code == 0 and idle.output.strip() == "idle"
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    with sessions() as database:
        jobs = database.query(JobRecord).all()
        assert len(jobs) == 1
        assert jobs[0].kind == "RETENTION" and jobs[0].state == "SUCCEEDED"
    engine.dispose()


def test_backup_cli_creates_verifies_and_applies_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    primary_root = tmp_path / "primary"
    backup_root = tmp_path / "backups"
    FilesystemObjectStore(primary_root).put_file(
        "qualification/object",
        BytesIO(b"safe qualification object"),
        maximum_bytes=1024,
    )
    keyring = tmp_path / "backup-keys"
    keyring.mkdir()
    (keyring / "backup-key-v1").write_text("62" * 32, encoding="ascii")
    dump_script = """
import sys
if "--version" in sys.argv:
    sys.stdout.write("pg_dump (PostgreSQL) 16.10\\n")
else:
    sys.stdout.buffer.write(b"PGDMP safe CLI dump")
"""
    restore_script = """
import sys
raise SystemExit(0 if sys.stdin.buffer.read(5) == b"PGDMP" else 1)
"""
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_ROOT", str(primary_root))
    monkeypatch.setenv("RATEREPLAY_BACKUP_OBJECT_STORE_ROOT", str(backup_root))
    monkeypatch.setenv("RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_KEYS_DIR", str(keyring))
    monkeypatch.setenv(
        "RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION",
        "backup-key-v1",
    )
    monkeypatch.setenv(
        "RATEREPLAY_BACKUP_PGDUMP_COMMAND_JSON",
        json.dumps([sys.executable, "-c", dump_script]),
    )
    monkeypatch.setenv(
        "RATEREPLAY_BACKUP_PGDUMP_VERSION_COMMAND_JSON",
        json.dumps([sys.executable, "-c", dump_script]),
    )
    monkeypatch.setenv(
        "RATEREPLAY_BACKUP_PGRESTORE_COMMAND_JSON",
        json.dumps([sys.executable, "-c", restore_script]),
    )

    runner = CliRunner()
    created = runner.invoke(app, ["create-backup"])

    assert created.exit_code == 0, created.output
    match = re.search(r"backup_id=([^ ]+)", created.output)
    assert match is not None
    backup_id = match.group(1)
    verified = runner.invoke(app, ["verify-backup", backup_id])
    retained = runner.invoke(app, ["expire-backups-once"])

    assert verified.exit_code == 0 and "verified=true" in verified.output
    assert retained.exit_code == 0 and "expired_backups=0" in retained.output
    persisted = b"".join(path.read_bytes() for path in backup_root.rglob("*") if path.is_file())
    assert b"PGDMP" not in persisted
    assert b"safe qualification object" not in persisted


def test_deletion_reconciler_requires_separate_control_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATEREPLAY_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.delenv("RATEREPLAY_DELETION_LEDGER_KEY_FILE", raising=False)
    monkeypatch.delenv("RATEREPLAY_RESTORE_KEY_FILE", raising=False)
    result = CliRunner().invoke(app, ["reconcile-deletions-once"])
    assert result.exit_code == 2
    assert "RATEREPLAY_DELETION_LEDGER_KEY_FILE is required" in result.output


def _configure_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    initialize_ledger: bool,
) -> tuple[Path, Path, FilesystemDeletionLedger | None]:
    database_path = tmp_path / "restore.sqlite"
    engine = make_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    ledger_root = tmp_path / "ledger"
    ledger_key_file = tmp_path / "ledger.key"
    restore_key_file = tmp_path / "restore.key"
    outcome_key_file = tmp_path / "outcome.key"
    ledger_key_file.write_bytes(b"l" * 32)
    restore_key_file.write_bytes(b"r" * 32)
    outcome_key_file.write_bytes(b"o" * 32)
    monkeypatch.setenv("RATEREPLAY_DATABASE_URL", f"sqlite+pysqlite:///{database_path}")
    monkeypatch.setenv("RATEREPLAY_DELETION_LEDGER_ROOT", str(ledger_root))
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("RATEREPLAY_DELETION_LEDGER_KEY_FILE", str(ledger_key_file))
    monkeypatch.setenv("RATEREPLAY_RESTORE_KEY_FILE", str(restore_key_file))
    monkeypatch.setenv("RATEREPLAY_TRANSACTION_OUTCOME_KEY_FILE", str(outcome_key_file))
    ledger = (
        FilesystemDeletionLedger(ledger_root, integrity_key=b"l" * 32)
        if initialize_ledger
        else None
    )
    return database_path, tmp_path / "qualification.json", ledger


def test_restore_qualification_cli_writes_and_self_verifies_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, artifact, _ = _configure_restore(monkeypatch, tmp_path, initialize_ledger=True)

    result = CliRunner().invoke(
        app,
        ["qualify-restore", "--artifact-file", str(artifact)],
    )

    assert result.exit_code == 0, result.output
    assert "exposure_allowed=true" in result.output
    assert artifact.stat().st_mode & 0o777 == 0o600
    verified = CliRunner().invoke(
        app,
        ["verify-restore-qualification", "--artifact-file", str(artifact)],
    )
    assert verified.exit_code == 0
    assert "exposure_allowed=true" in verified.output


def test_restore_qualification_cli_writes_hold_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path, artifact, ledger = _configure_restore(
        monkeypatch,
        tmp_path,
        initialize_ledger=True,
    )
    assert ledger is not None
    scope_id = "2" * 32
    engine = make_engine(f"sqlite+pysqlite:///{database_path}")
    sessions = make_session_factory(engine)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id="1" * 32,
                username_canonical="held-restore",
                password_hash="test-only",
                deletion_scope_id=scope_id,
                created_at=datetime.now(UTC),
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
    engine.dispose()
    ledger.append(
        deletion_id="3" * 32,
        phase="PREPARED",
        scope_token=_scope_token(b"r" * 32, scope_id),
        restore_key_version="restore-v1",
        original_generation=0,
        proposed_generation=1,
        preparation_digest="4" * 64,
        intent_proof_digest="5" * 64,
        occurred_at=datetime.now(UTC),
    )

    result = CliRunner().invoke(
        app,
        ["qualify-restore", "--artifact-file", str(artifact)],
    )

    assert result.exit_code == 3
    assert "exposure_allowed=false" in result.output
    assert artifact.is_file()
    verified = CliRunner().invoke(
        app,
        ["verify-restore-qualification", "--artifact-file", str(artifact)],
    )
    assert verified.exit_code == 3


def test_restore_qualification_cli_rejects_missing_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, artifact, _ = _configure_restore(monkeypatch, tmp_path, initialize_ledger=False)

    result = CliRunner().invoke(
        app,
        ["qualify-restore", "--artifact-file", str(artifact)],
    )

    assert result.exit_code == 1
    assert "LEDGER_MISSING" in result.output
    assert not artifact.exists()
