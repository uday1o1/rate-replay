import hashlib
import hmac
import json
import re
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from ratereplay_domain.telemetry import Telemetry, TelemetryConfiguration
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletions import DeletionCoordinator, _scope_token
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.keyrings import VersionedKeyring
from ratereplay_persistence.models import JobRecord, UserRecord
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.retention import RetentionScheduler
from ratereplay_worker import cli as worker_cli
from ratereplay_worker.cli import app
from sqlalchemy import text
from sqlalchemy.engine import Engine
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
    assert "run-all" in result.output
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
    assert "restore-backup-to-quarantine" in result.output
    assert "rotate-deletion-keys" in result.output
    assert "migrate-deletion-ledger-v1" in result.output
    assert "verify-restore-qualification" in result.output
    assert "verify-restore-exposure" in result.output


def test_run_all_poll_updates_fixed_metrics_and_every_worker() -> None:
    class FakeWorker:
        def __init__(self, processed: bool) -> None:
            self.processed = processed
            self.calls = 0

        def run_once(self, *, now: datetime) -> bool:
            assert now.tzinfo is not None
            self.calls += 1
            return self.processed

    class FakeJobs:
        def operational_snapshots(self, *, now: datetime) -> tuple[SimpleNamespace, ...]:
            assert now.tzinfo is not None
            return (
                SimpleNamespace(
                    kind="SCENARIO",
                    queue_depth=2,
                    oldest_lease_age_seconds=3.5,
                    retry_attempts=1,
                ),
            )

    class FakeCoordinator:
        calls = 0

        def reconcile(self, *, now: datetime) -> None:
            assert now.tzinfo is not None
            self.calls += 1

    class FakeScheduler:
        schedule_calls = 0
        raw_calls = 0

        def schedule(self, *, now: datetime) -> None:
            assert now.tzinfo is not None
            self.schedule_calls += 1

        def schedule_raw_expirations(self, *, now: datetime) -> None:
            assert now.tzinfo is not None
            self.raw_calls += 1

    telemetry = Telemetry(
        TelemetryConfiguration(service_name="ratereplay-worker", environment="test")
    )
    first = FakeWorker(True)
    second = FakeWorker(False)
    coordinator = FakeCoordinator()
    scheduler = FakeScheduler()
    try:
        processed = worker_cli._poll_all_once(
            telemetry,
            (("IMPORT", first), ("REPORT", second)),
            cast(JobService, FakeJobs()),
            cast(DeletionCoordinator, coordinator),
            cast(RetentionScheduler, scheduler),
            schedule_retention=True,
            now=datetime.now(UTC),
        )
        metrics = telemetry.prometheus_bytes().decode("utf-8")
    finally:
        telemetry.shutdown()
    assert processed
    assert first.calls == second.calls == 1
    assert coordinator.calls == 1
    assert scheduler.schedule_calls == scheduler.raw_calls == 1
    assert 'kind="SCENARIO"' in metrics
    assert 'kind="IMPORT",outcome="processed"' in metrics
    assert 'kind="REPORT",outcome="idle"' in metrics


def test_worker_metrics_listener_configuration_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATEREPLAY_WORKER_METRICS_PORT", "0")
    with pytest.raises(RuntimeError, match="between 1 and 65535"):
        worker_cli._worker_metrics_port()
    monkeypatch.setenv("RATEREPLAY_WORKER_METRICS_ADDRESS", "public.example")
    with pytest.raises(RuntimeError, match=r"127\.0\.0\.1 or 0\.0\.0\.0"):
        worker_cli._worker_metrics_address()


def test_run_all_cli_starts_every_component_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWorker:
        calls = 0

        def run_once(self, *, now: datetime) -> bool:
            assert now.tzinfo is not None
            self.calls += 1
            return False

    class FakeJobs:
        def operational_snapshots(self, *, now: datetime) -> tuple[SimpleNamespace, ...]:
            assert now.tzinfo is not None
            return ()

    class FakeCoordinator:
        calls = 0

        def reconcile(self, *, now: datetime) -> None:
            assert now.tzinfo is not None
            self.calls += 1

    class FakeScheduler:
        schedule_calls = 0
        raw_calls = 0

        def schedule(self, *, now: datetime) -> None:
            assert now.tzinfo is not None
            self.schedule_calls += 1

        def schedule_raw_expirations(self, *, now: datetime) -> None:
            assert now.tzinfo is not None
            self.raw_calls += 1

    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeMetricsServer:
        shutdown_called = False
        close_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

        def server_close(self) -> None:
            self.close_called = True

    workers: list[FakeWorker] = []
    engines: list[FakeEngine] = []

    def configured_worker(_telemetry: Telemetry) -> tuple[FakeWorker, Engine]:
        worker = FakeWorker()
        engine = FakeEngine()
        workers.append(worker)
        engines.append(engine)
        return worker, cast(Engine, engine)

    coordinator = FakeCoordinator()
    scheduler = FakeScheduler()

    def configured_coordinator() -> tuple[DeletionCoordinator, Engine]:
        engine = FakeEngine()
        engines.append(engine)
        return cast(DeletionCoordinator, coordinator), cast(Engine, engine)

    def configured_scheduler() -> tuple[RetentionScheduler, Engine]:
        engine = FakeEngine()
        engines.append(engine)
        return cast(RetentionScheduler, scheduler), cast(Engine, engine)

    metrics_server = FakeMetricsServer()
    telemetry = Telemetry(
        TelemetryConfiguration(service_name="ratereplay-worker", environment="test")
    )
    monkeypatch.setattr(worker_cli, "_configured_telemetry", lambda: telemetry)
    for name in (
        "_configured_worker",
        "_configured_deletion_worker",
        "_configured_retention_worker",
        "_configured_report_worker",
        "_configured_replay_worker",
        "_configured_comparison_worker",
        "_configured_scenario_worker",
    ):
        monkeypatch.setattr(worker_cli, name, configured_worker)
    monkeypatch.setattr(worker_cli, "_configured_deletion_reconciler", configured_coordinator)
    monkeypatch.setattr(worker_cli, "_configured_retention_scheduler", configured_scheduler)
    monkeypatch.setattr(worker_cli, "make_session_factory", lambda _engine: object())
    monkeypatch.setattr(worker_cli, "JobService", lambda _sessions: FakeJobs())
    monkeypatch.setattr(
        worker_cli,
        "start_http_server",
        lambda _port, **_kwargs: (metrics_server, object()),
    )

    result = CliRunner().invoke(app, ["run-all", "--once"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "idle"
    assert len(workers) == 7 and all(worker.calls == 1 for worker in workers)
    assert coordinator.calls == 1
    assert scheduler.schedule_calls == scheduler.raw_calls == 1
    assert len(engines) == 9 and all(engine.disposed for engine in engines)
    assert metrics_server.shutdown_called and metrics_server.close_called


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


def test_quarantine_restore_cli_binds_backup_database_objects_and_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source-objects"
    backup_root = tmp_path / "backups"
    FilesystemObjectStore(source_root).put_file(
        "owners/private-user/input.xml",
        BytesIO(b"restored private object"),
        maximum_bytes=1024,
    )
    backup_keys = tmp_path / "backup-keys"
    backup_keys.mkdir()
    (backup_keys / "backup-key-v1").write_text("62" * 32, encoding="ascii")
    dump_script = """
import sys
if "--version" in sys.argv:
    sys.stdout.write("pg_dump (PostgreSQL) 16.10\\n")
else:
    sys.stdout.buffer.write(b"PGDMP verified quarantine dump")
"""
    restore_script = """
import sys
raise SystemExit(0 if sys.stdin.buffer.read(5) == b"PGDMP" else 1)
"""
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_ROOT", str(source_root))
    monkeypatch.setenv("RATEREPLAY_BACKUP_OBJECT_STORE_ROOT", str(backup_root))
    monkeypatch.setenv("RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_KEYS_DIR", str(backup_keys))
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

    database_path, _, ledger = _configure_restore(
        monkeypatch,
        tmp_path,
        initialize_ledger=True,
    )
    assert ledger is not None
    database = make_engine(f"sqlite+pysqlite:///{database_path}")
    with database.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(128))"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0014_restore_controls')")
        )
    database.dispose()
    qualification_artifact = tmp_path / "evidence" / "qualification.json"
    exposure_artifact = tmp_path / "evidence" / "exposure.json"
    result = runner.invoke(
        app,
        [
            "restore-backup-to-quarantine",
            backup_id,
            "--materialization-directory",
            str(tmp_path / "materialized"),
            "--qualification-artifact-file",
            str(qualification_artifact),
            "--exposure-artifact-file",
            str(exposure_artifact),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "exposure_allowed=true" in result.output
    with FilesystemObjectStore(tmp_path / "objects").open_file(
        "owners/private-user/input.xml",
        maximum_bytes=1024,
    ) as restored_object:
        assert restored_object.read() == b"restored private object"
    exposure_payload = json.loads(exposure_artifact.read_text(encoding="ascii"))
    qualification_payload = json.loads(qualification_artifact.read_text(encoding="ascii"))
    assert exposure_payload["backup_id"] == backup_id
    assert exposure_payload["database_revision"] == "0014_restore_controls"
    assert (
        exposure_payload["qualification_artifact_sha256"]
        == qualification_payload["artifact_sha256"]
    )
    assert exposure_payload["restored_object_count"] == 1
    assert "private-user" not in exposure_artifact.read_text(encoding="ascii")
    verified = runner.invoke(
        app,
        ["verify-restore-exposure", "--artifact-file", str(exposure_artifact)],
    )
    assert verified.exit_code == 0, verified.output
    assert f"backup_id={backup_id}" in verified.output

    held_scope_id = "7" * 32
    database = make_engine(f"sqlite+pysqlite:///{database_path}")
    with database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, username_canonical, password_hash, created_at, lifecycle_state, "
                "lifecycle_generation, deletion_scope_id) "
                "VALUES (:id, 'held-quarantine', 'test-only', :created_at, 'ACTIVE', 0, :scope)"
            ),
            {
                "id": "8" * 32,
                "created_at": datetime.now(UTC),
                "scope": held_scope_id,
            },
        )
    database.dispose()
    ledger.append(
        deletion_id="9" * 32,
        phase="PREPARED",
        scope_token=_scope_token(b"r" * 32, held_scope_id),
        restore_key_version="restore-v1",
        original_generation=0,
        proposed_generation=1,
        preparation_digest="a" * 64,
        intent_proof_digest="b" * 64,
        occurred_at=datetime.now(UTC),
    )
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_ROOT", str(tmp_path / "held-objects"))
    held_qualification = tmp_path / "evidence" / "held-qualification.json"
    held_exposure = tmp_path / "evidence" / "held-exposure.json"
    held = runner.invoke(
        app,
        [
            "restore-backup-to-quarantine",
            backup_id,
            "--materialization-directory",
            str(tmp_path / "materialized-held"),
            "--qualification-artifact-file",
            str(held_qualification),
            "--exposure-artifact-file",
            str(held_exposure),
        ],
    )
    assert held.exit_code == 3, held.output
    assert "exposure_allowed=false" in held.output
    assert held_qualification.is_file()
    assert held_exposure.is_file()

    class ChangedRestoreRunner:
        def restore(self, dump_path: Path) -> str:
            assert dump_path.is_file()
            return "0" * 64

    monkeypatch.setattr(
        worker_cli,
        "_configured_postgres_restore_runner",
        lambda: ChangedRestoreRunner(),
    )
    changed = runner.invoke(
        app,
        [
            "restore-backup-to-quarantine",
            backup_id,
            "--materialization-directory",
            str(tmp_path / "materialized-changed"),
            "--qualification-artifact-file",
            str(tmp_path / "evidence" / "changed-qualification.json"),
            "--exposure-artifact-file",
            str(tmp_path / "evidence" / "changed-exposure.json"),
        ],
    )
    assert changed.exit_code == 1
    assert "PG_RESTORE_INPUT_CHANGED" in changed.output


def test_restore_qualification_cli_accepts_versioned_key_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, artifact, _ = _configure_restore(monkeypatch, tmp_path, initialize_ledger=True)
    ledger_keys = tmp_path / "ledger-keys"
    restore_keys = tmp_path / "restore-keys"
    ledger_keys.mkdir()
    restore_keys.mkdir()
    (ledger_keys / "ledger-v1").write_bytes(b"l" * 32)
    (restore_keys / "restore-v1").write_bytes(b"r" * 32)
    monkeypatch.delenv("RATEREPLAY_DELETION_LEDGER_KEY_FILE")
    monkeypatch.delenv("RATEREPLAY_RESTORE_KEY_FILE")
    monkeypatch.setenv("RATEREPLAY_DELETION_LEDGER_KEYS_DIR", str(ledger_keys))
    monkeypatch.setenv("RATEREPLAY_DELETION_LEDGER_CURRENT_KEY_VERSION", "ledger-v1")
    monkeypatch.setenv("RATEREPLAY_RESTORE_KEYS_DIR", str(restore_keys))
    monkeypatch.setenv("RATEREPLAY_RESTORE_CURRENT_KEY_VERSION", "restore-v1")

    result = CliRunner().invoke(
        app,
        ["qualify-restore", "--artifact-file", str(artifact)],
    )

    assert result.exit_code == 0, result.output
    assert "exposure_allowed=true" in result.output


def test_deletion_key_rotation_cli_writes_verified_redacted_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ledger"
    FilesystemDeletionLedger(
        root,
        integrity_key=b"l" * 32,
        restore_key_version="restore-v1",
    )
    ledger_keys = tmp_path / "ledger-keys"
    restore_keys = tmp_path / "restore-keys"
    ledger_keys.mkdir()
    restore_keys.mkdir()
    (ledger_keys / "ledger-v1").write_bytes(b"l" * 32)
    (ledger_keys / "ledger-v2").write_bytes(b"n" * 32)
    (restore_keys / "restore-v1").write_bytes(b"r" * 32)
    (restore_keys / "restore-v2").write_bytes(b"s" * 32)
    expected_head = hashlib.sha256((root / "deletion-ledger-head-v2.json").read_bytes()).hexdigest()
    artifact = tmp_path / "rotation.json"

    result = CliRunner().invoke(
        app,
        [
            "rotate-deletion-keys",
            "--root",
            str(root),
            "--keys-dir",
            str(ledger_keys),
            "--restore-keys-dir",
            str(restore_keys),
            "--expected-ledger-key-version",
            "ledger-v1",
            "--new-ledger-key-version",
            "ledger-v2",
            "--expected-restore-key-version",
            "restore-v1",
            "--new-restore-key-version",
            "restore-v2",
            "--expected-head-sha256",
            expected_head,
            "--artifact-file",
            str(artifact),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ledger_key_version=ledger-v2" in result.output
    assert "restore_key_version=restore-v2" in result.output
    payload = json.loads(artifact.read_text(encoding="ascii"))
    assert payload["previous_head_sha256"] == expected_head
    assert b"r" * 32 not in artifact.read_bytes()
    FilesystemDeletionLedger(
        root,
        keyring=VersionedKeyring(
            current_version="ledger-v2",
            keys={"ledger-v1": b"l" * 32, "ledger-v2": b"n" * 32},
        ),
        restore_key_version="restore-v2",
        require_existing=True,
    ).validate()


def test_plaintext_ledger_migration_cli_publishes_encrypted_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    destination = tmp_path / "encrypted"
    source.mkdir()
    legacy_key = b"k" * 32
    genesis_receipt = hmac.new(
        legacy_key,
        b"RateReplay.DeletionLedgerGenesis.v1\x00",
        hashlib.sha256,
    ).hexdigest()
    (source / "deletion-ledger-genesis-v1.json").write_text(
        json.dumps(
            {
                "schema_version": "deletion-ledger-genesis-v1",
                "receipt": genesis_receipt,
            }
        )
        + "\n",
        encoding="ascii",
    )
    (source / "deletion-ledger-v1.jsonl").write_text("", encoding="ascii")
    legacy_key_file = tmp_path / "legacy.key"
    legacy_key_file.write_bytes(legacy_key)
    ledger_keys = tmp_path / "ledger-keys"
    restore_keys = tmp_path / "restore-keys"
    ledger_keys.mkdir()
    restore_keys.mkdir()
    (ledger_keys / "ledger-v2").write_bytes(b"n" * 32)
    (restore_keys / "restore-v1").write_bytes(b"r" * 32)
    artifact = tmp_path / "migration.json"

    result = CliRunner().invoke(
        app,
        [
            "migrate-deletion-ledger-v1",
            "--source-root",
            str(source),
            "--destination-root",
            str(destination),
            "--legacy-integrity-key-file",
            str(legacy_key_file),
            "--ledger-keys-dir",
            str(ledger_keys),
            "--ledger-current-key-version",
            "ledger-v2",
            "--restore-keys-dir",
            str(restore_keys),
            "--restore-current-key-version",
            "restore-v1",
            "--artifact-file",
            str(artifact),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "migrated_events=0" in result.output
    assert artifact.is_file()
    assert (source / "deletion-ledger-v1.jsonl").read_bytes() == b""
    FilesystemDeletionLedger(
        destination,
        keyring=VersionedKeyring.single("ledger-v2", b"n" * 32),
        restore_key_version="restore-v1",
        require_existing=True,
    ).validate()


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
