"""Public durable-worker command line entry point."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

import typer
from prometheus_client import start_http_server
from ratereplay_domain.environment import environment_lock_hash
from ratereplay_domain.telemetry import Telemetry, TelemetryConfiguration
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.backups import (
    BackupError,
    BackupRetentionService,
    BackupRuntimeConfiguration,
    BackupService,
    PostgresDumpRunner,
    PostgresRestoreRunner,
)
from ratereplay_persistence.database import (
    DatabaseAtRestConfiguration,
    make_engine,
    make_session_factory,
)
from ratereplay_persistence.deletion_ledger import (
    DeletionLedgerError,
    FilesystemDeletionLedger,
    verify_rotation_artifact,
    write_rotation_artifact,
)
from ratereplay_persistence.deletion_ledger_migration import (
    migrate_plaintext_v1_ledger,
    verify_migration_artifact,
    write_migration_artifact,
)
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.deletions import DeletionCoordinator
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.keyrings import KeyringError, VersionedKeyring, load_keyring
from ratereplay_persistence.object_store import (
    ObjectStore,
    ObjectStoreConfiguration,
    ObjectStoreError,
)
from ratereplay_persistence.restore import (
    RestoreQualificationError,
    RestoreReconciler,
    TransactionOutcomeEvidence,
    verify_restore_qualification_artifact,
    write_restore_qualification_artifact,
)
from ratereplay_persistence.restore_evidence import (
    bind_restore_exposure,
    verify_restore_exposure_artifact,
    write_restore_exposure_artifact,
)
from ratereplay_persistence.retention import DatabaseRetentionService, RetentionScheduler
from ratereplay_tariffs.admission import load_all_admitted_tariffs
from ratereplay_tariffs.comparison import load_required_component_keys
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ratereplay_worker.comparison_worker import ComparisonWorker
from ratereplay_worker.deletion_worker import DeletionWorker
from ratereplay_worker.import_worker import ImportWorker
from ratereplay_worker.replay_worker import ReplayWorker
from ratereplay_worker.report_worker import ReportWorker
from ratereplay_worker.retention_worker import RetentionWorker
from ratereplay_worker.scenario_worker import ScenarioWorker

app = typer.Typer(no_args_is_help=True)
WORKER_POLL_SECONDS = 1.0
RETENTION_SCHEDULER_POLL_SECONDS = 60.0


def _configured_telemetry() -> Telemetry:
    return Telemetry(
        TelemetryConfiguration.from_environment(
            service_name="ratereplay-worker",
            environment=os.getenv("RATEREPLAY_ENV", "development"),
        )
    )


def _worker_metrics_port() -> int:
    configured = os.getenv("RATEREPLAY_WORKER_METRICS_PORT", "9100")
    try:
        port = int(configured)
    except ValueError as error:
        raise RuntimeError("RATEREPLAY_WORKER_METRICS_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("RATEREPLAY_WORKER_METRICS_PORT must be between 1 and 65535")
    return port


def _worker_metrics_address() -> str:
    address = os.getenv("RATEREPLAY_WORKER_METRICS_ADDRESS", "127.0.0.1")
    if address not in {"0.0.0.0", "127.0.0.1"}:  # noqa: S104  # nosec B104
        raise RuntimeError("RATEREPLAY_WORKER_METRICS_ADDRESS must be 127.0.0.1 or 0.0.0.0")
    return address


class PollableWorker(Protocol):
    def run_once(self, *, now: datetime) -> bool: ...


class MetricsServer(Protocol):
    def shutdown(self) -> None: ...

    def server_close(self) -> None: ...


def _run_worker_poll(telemetry: Telemetry, kind: str, worker: PollableWorker) -> bool:
    return telemetry.run_worker(kind, lambda: worker.run_once(now=datetime.now(UTC)))


def _poll_all_once(
    telemetry: Telemetry,
    workers: tuple[tuple[str, PollableWorker], ...],
    jobs: JobService,
    coordinator: DeletionCoordinator,
    scheduler: RetentionScheduler,
    *,
    schedule_retention: bool,
    now: datetime,
) -> bool:
    coordinator.reconcile(now=now)
    if schedule_retention:
        scheduler.schedule(now=now)
        scheduler.schedule_raw_expirations(now=now)
    for snapshot in jobs.operational_snapshots(now=now):
        telemetry.set_job_snapshot(
            kind=snapshot.kind,
            queue_depth=snapshot.queue_depth,
            oldest_lease_age_seconds=snapshot.oldest_lease_age_seconds,
            retry_attempts=snapshot.retry_attempts,
        )
    processed = False
    for kind, worker in workers:
        if _run_worker_poll(telemetry, kind, worker):
            processed = True
    return processed


@app.callback()
def main() -> None:
    """Run durable RateReplay worker operations."""

    DatabaseAtRestConfiguration.from_environment(
        environment=os.getenv("RATEREPLAY_ENV", "development")
    )


def _configured_object_store() -> ObjectStore:
    return _object_store_configuration().build(
        ensure_bucket=os.getenv("RATEREPLAY_ENV", "development") == "development"
    )


def _object_store_configuration() -> ObjectStoreConfiguration:
    return ObjectStoreConfiguration.from_environment(
        environment=os.getenv("RATEREPLAY_ENV", "development"),
        default_root=Path("/var/lib/ratereplay/objects"),
    )


def _backup_runtime_configuration(
    primary_store: ObjectStoreConfiguration,
) -> BackupRuntimeConfiguration:
    return BackupRuntimeConfiguration.from_environment(
        environment=os.getenv("RATEREPLAY_ENV", "development"),
        primary_store=primary_store,
        default_root=Path("/var/lib/ratereplay/backups"),
    )


def _configured_backup_service() -> BackupService:
    primary_configuration = _object_store_configuration()
    backup_configuration = _backup_runtime_configuration(primary_configuration)
    ensure_bucket = os.getenv("RATEREPLAY_ENV", "development") == "development"
    return BackupService(
        source_objects=primary_configuration.build(ensure_bucket=ensure_bucket),
        backup_objects=backup_configuration.store.build(ensure_bucket=ensure_bucket),
        database_dumper=PostgresDumpRunner(backup_configuration.postgres),
        database_maximum_bytes=backup_configuration.postgres.maximum_bytes,
        source_object_maximum_bytes=backup_configuration.source_object_maximum_bytes,
    )


def _configured_postgres_restore_runner() -> PostgresRestoreRunner:
    primary_configuration = _object_store_configuration()
    backup_configuration = _backup_runtime_configuration(primary_configuration)
    return PostgresRestoreRunner(backup_configuration.postgres)


def _configured_backup_retention(
    primary_configuration: ObjectStoreConfiguration,
) -> BackupRetentionService | None:
    environment = os.getenv("RATEREPLAY_ENV", "development")
    configured = any(
        os.getenv(variable) is not None
        for variable in (
            "RATEREPLAY_BACKUP_OBJECT_STORE_BACKEND",
            "RATEREPLAY_BACKUP_OBJECT_STORE_ROOT",
            "RATEREPLAY_BACKUP_S3_ENDPOINT",
            "RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_KEYS_DIR",
            "RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION",
        )
    )
    if environment not in {"production", "staging"} and not configured:
        return None
    backup_configuration = _backup_runtime_configuration(primary_configuration)
    return BackupRetentionService(
        backup_configuration.store.build(ensure_bucket=environment == "development")
    )


def _configured_worker(telemetry: Telemetry | None = None) -> tuple[ImportWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    schema_path = Path(
        os.getenv(
            "RATEREPLAY_ESPI_SCHEMA_PATH",
            "third_party/espi-schema/espi-4.0.xsd",
        )
    )
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    imports = ImportService(sessions, _configured_object_store())
    worker = ImportWorker(
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
        jobs=JobService(sessions),
        imports=imports,
        espi_schema_path=schema_path,
        telemetry=telemetry,
    )
    return worker, engine


def _configured_deletion_reconciler() -> tuple[DeletionCoordinator, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    ledger_root = Path(
        os.getenv("RATEREPLAY_DELETION_LEDGER_ROOT", "/var/lib/ratereplay/deletion-ledger")
    )
    ledger_keyring = _required_keyring(
        directory_variable="RATEREPLAY_DELETION_LEDGER_KEYS_DIR",
        current_version_variable="RATEREPLAY_DELETION_LEDGER_CURRENT_KEY_VERSION",
        legacy_file_variable="RATEREPLAY_DELETION_LEDGER_KEY_FILE",
        default_version="ledger-v1",
    )
    restore_keyring = _required_keyring(
        directory_variable="RATEREPLAY_RESTORE_KEYS_DIR",
        current_version_variable="RATEREPLAY_RESTORE_CURRENT_KEY_VERSION",
        legacy_file_variable="RATEREPLAY_RESTORE_KEY_FILE",
        default_version=os.getenv("RATEREPLAY_RESTORE_KEY_VERSION", "restore-v1"),
    )
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    coordinator = DeletionCoordinator(
        sessions,
        FilesystemDeletionLedger(
            ledger_root,
            keyring=ledger_keyring,
            restore_key_version=restore_keyring.current_version,
            actor="PREPARATION_RECONCILER",
        ),
        restore_keyring=restore_keyring,
    )
    return coordinator, engine


def _configured_deletion_worker(
    telemetry: Telemetry | None = None,
) -> tuple[DeletionWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    ledger_root = Path(
        os.getenv("RATEREPLAY_DELETION_LEDGER_ROOT", "/var/lib/ratereplay/deletion-ledger")
    )
    ledger_keyring = _required_ledger_keyring()
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    jobs = JobService(sessions)
    ledger = FilesystemDeletionLedger(
        ledger_root,
        keyring=ledger_keyring,
        restore_key_version=_restore_current_version(),
        actor="DELETION_WORKER",
    )
    worker = DeletionWorker(
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
        jobs=jobs,
        sweeps=DeletionSweepService(
            sessions,
            _configured_object_store(),
            ledger,
        ),
        telemetry=telemetry,
    )
    return worker, engine


def _configured_retention_scheduler() -> tuple[RetentionScheduler, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    engine = make_engine(database_url)
    return RetentionScheduler(make_session_factory(engine)), engine


def _configured_retention_worker(
    telemetry: Telemetry | None = None,
) -> tuple[RetentionWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    ledger_root = Path(
        os.getenv("RATEREPLAY_DELETION_LEDGER_ROOT", "/var/lib/ratereplay/deletion-ledger")
    )
    ledger_keyring = _required_ledger_keyring()
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    primary_configuration = _object_store_configuration()
    objects = primary_configuration.build(
        ensure_bucket=os.getenv("RATEREPLAY_ENV", "development") == "development"
    )
    ledger = FilesystemDeletionLedger(
        ledger_root,
        keyring=ledger_keyring,
        restore_key_version=_restore_current_version(),
        actor="RETENTION_WORKER",
    )
    return (
        RetentionWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            imports=ImportService(sessions, objects),
            artifacts=ArtifactService(sessions, objects),
            deletions=DeletionSweepService(sessions, objects, ledger),
            database_retention=DatabaseRetentionService(sessions, ledger),
            backup_retention=_configured_backup_retention(primary_configuration),
            telemetry=telemetry,
        ),
        engine,
    )


def _configured_restore_reconciler() -> tuple[RestoreReconciler, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    ledger_root = Path(
        os.getenv("RATEREPLAY_DELETION_LEDGER_ROOT", "/var/lib/ratereplay/deletion-ledger")
    )
    ledger_keyring = _required_ledger_keyring()
    restore_keyring = _required_restore_keyring()
    outcome_key = _required_key_file("RATEREPLAY_TRANSACTION_OUTCOME_KEY_FILE")
    ledger = FilesystemDeletionLedger(
        ledger_root,
        keyring=ledger_keyring,
        restore_key_version=restore_keyring.current_version,
        actor="RESTORE_QUALIFIER",
        require_existing=True,
    )
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    return (
        RestoreReconciler(
            sessions,
            _configured_object_store(),
            ledger,
            restore_keyring=restore_keyring,
            outcome_evidence_key=outcome_key,
        ),
        engine,
    )


def _restored_database_revision(engine: Engine) -> str:
    try:
        with engine.connect() as database:
            revision = database.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    except (SQLAlchemyError, TypeError, ValueError) as error:
        raise RestoreQualificationError(
            "DATABASE_REVISION_UNVERIFIED",
            "Restored database revision could not be verified",
        ) from error
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise RestoreQualificationError(
            "DATABASE_REVISION_UNVERIFIED",
            "Restored database revision is invalid",
        )
    return revision


def _configured_report_worker(telemetry: Telemetry | None = None) -> tuple[ReportWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    objects = _configured_object_store()
    return (
        ReportWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, objects),
            telemetry=telemetry,
        ),
        engine,
    )


def _configured_replay_worker(telemetry: Telemetry | None = None) -> tuple[ReplayWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    repository_root = Path(os.getenv("RATEREPLAY_REPOSITORY_ROOT", ".")).resolve()
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    tariffs = load_all_admitted_tariffs(repository_root)
    return (
        ReplayWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, _configured_object_store()),
            admitted_tariffs={item.lock.tariff_version_id: item for item in tariffs},
            environment_lock_hash=environment_lock_hash(repository_root),
            telemetry=telemetry,
        ),
        engine,
    )


def _configured_comparison_worker(
    telemetry: Telemetry | None = None,
) -> tuple[ComparisonWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    repository_root = Path(os.getenv("RATEREPLAY_REPOSITORY_ROOT", ".")).resolve()
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    tariffs = load_all_admitted_tariffs(repository_root)
    return (
        ComparisonWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, _configured_object_store()),
            admitted_tariffs={item.lock.tariff_version_id: item for item in tariffs},
            required_component_keys=load_required_component_keys(repository_root),
            environment_lock_hash=environment_lock_hash(repository_root),
            telemetry=telemetry,
        ),
        engine,
    )


def _configured_scenario_worker(
    telemetry: Telemetry | None = None,
) -> tuple[ScenarioWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    repository_root = Path(os.getenv("RATEREPLAY_REPOSITORY_ROOT", ".")).resolve()
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    tariffs = load_all_admitted_tariffs(repository_root)
    return (
        ScenarioWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, _configured_object_store()),
            admitted_tariffs={item.lock.tariff_version_id: item for item in tariffs},
            environment_lock_hash=environment_lock_hash(repository_root),
            telemetry=telemetry,
        ),
        engine,
    )


def _required_key_file(variable: str) -> bytes:
    path = os.getenv(variable)
    if path is None:
        typer.echo(f"{variable} is required", err=True)
        raise typer.Exit(code=2)
    try:
        value = Path(path).read_bytes().strip()
    except OSError:
        typer.echo(f"{variable} cannot be read", err=True)
        raise typer.Exit(code=2) from None
    if len(value) != 32:
        typer.echo(f"{variable} must reference exactly 32 bytes", err=True)
        raise typer.Exit(code=2)
    return value


def _required_keyring(
    *,
    directory_variable: str,
    current_version_variable: str,
    legacy_file_variable: str,
    default_version: str,
) -> VersionedKeyring:
    directory = os.getenv(directory_variable)
    current_version = os.getenv(current_version_variable, default_version)
    if directory is None:
        return VersionedKeyring.single(
            current_version,
            _required_key_file(legacy_file_variable),
        )
    if os.getenv(legacy_file_variable) is not None:
        typer.echo(
            f"Configure {directory_variable} or {legacy_file_variable}, not both",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        return load_keyring(Path(directory), current_version=current_version)
    except KeyringError as error:
        typer.echo(f"{directory_variable} is invalid: {error.code}", err=True)
        raise typer.Exit(code=2) from error


def _required_ledger_keyring() -> VersionedKeyring:
    return _required_keyring(
        directory_variable="RATEREPLAY_DELETION_LEDGER_KEYS_DIR",
        current_version_variable="RATEREPLAY_DELETION_LEDGER_CURRENT_KEY_VERSION",
        legacy_file_variable="RATEREPLAY_DELETION_LEDGER_KEY_FILE",
        default_version="ledger-v1",
    )


def _required_restore_keyring() -> VersionedKeyring:
    return _required_keyring(
        directory_variable="RATEREPLAY_RESTORE_KEYS_DIR",
        current_version_variable="RATEREPLAY_RESTORE_CURRENT_KEY_VERSION",
        legacy_file_variable="RATEREPLAY_RESTORE_KEY_FILE",
        default_version=os.getenv("RATEREPLAY_RESTORE_KEY_VERSION", "restore-v1"),
    )


def _restore_current_version() -> str:
    return os.getenv(
        "RATEREPLAY_RESTORE_CURRENT_KEY_VERSION",
        os.getenv("RATEREPLAY_RESTORE_KEY_VERSION", "restore-v1"),
    )


def _read_outcome_evidence(path: Path | None) -> tuple[TransactionOutcomeEvidence, ...]:
    if path is None:
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise TypeError
        return tuple(TransactionOutcomeEvidence.from_dict(item) for item in payload)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise RestoreQualificationError(
            "OUTCOME_EVIDENCE_INVALID",
            "Transaction outcome evidence file is unreadable or invalid",
        ) from error


@app.command("run-once")
def run_once() -> None:
    """Lease and process at most one durable import job."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_worker(telemetry)
    try:
        processed = _run_worker_poll(telemetry, "IMPORT", worker)
        typer.echo("processed" if processed else "idle")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run")
def run() -> None:
    """Poll continuously for durable import jobs."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_worker(telemetry)
    try:
        while True:
            processed = _run_worker_poll(telemetry, "IMPORT", worker)
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-all")
def run_all(
    once: Annotated[
        bool,
        typer.Option(help="Poll every durable worker exactly once, then exit."),
    ] = False,
) -> None:
    """Run every durable worker, reconciliation, retention scheduling, and metrics."""

    telemetry = _configured_telemetry()
    engines: list[Engine] = []
    metrics_server: MetricsServer | None = None
    try:
        import_worker, import_engine = _configured_worker(telemetry)
        engines.append(import_engine)
        deletion_worker, deletion_engine = _configured_deletion_worker(telemetry)
        engines.append(deletion_engine)
        retention_worker, retention_engine = _configured_retention_worker(telemetry)
        engines.append(retention_engine)
        report_worker, report_engine = _configured_report_worker(telemetry)
        engines.append(report_engine)
        replay_worker, replay_engine = _configured_replay_worker(telemetry)
        engines.append(replay_engine)
        comparison_worker, comparison_engine = _configured_comparison_worker(telemetry)
        engines.append(comparison_engine)
        scenario_worker, scenario_engine = _configured_scenario_worker(telemetry)
        engines.append(scenario_engine)
        coordinator, reconciliation_engine = _configured_deletion_reconciler()
        engines.append(reconciliation_engine)
        scheduler, scheduler_engine = _configured_retention_scheduler()
        engines.append(scheduler_engine)
        workers: tuple[tuple[str, PollableWorker], ...] = (
            ("IMPORT", import_worker),
            ("DELETION", deletion_worker),
            ("RETENTION", retention_worker),
            ("REPORT", report_worker),
            ("REPLAY", replay_worker),
            ("COMPARISON", comparison_worker),
            ("SCENARIO", scenario_worker),
        )
        jobs = JobService(make_session_factory(import_engine))
        metrics_port = _worker_metrics_port()
        metrics_address = _worker_metrics_address()
        server, _thread = start_http_server(
            metrics_port,
            addr=metrics_address,
            registry=telemetry.registry,
        )
        metrics_server = server
        next_retention_schedule = 0.0
        while True:
            monotonic_now = time.monotonic()
            schedule_retention = monotonic_now >= next_retention_schedule
            processed = _poll_all_once(
                telemetry,
                workers,
                jobs,
                coordinator,
                scheduler,
                schedule_retention=schedule_retention,
                now=datetime.now(UTC),
            )
            if schedule_retention:
                next_retention_schedule = monotonic_now + RETENTION_SCHEDULER_POLL_SECONDS
            if once:
                typer.echo("processed" if processed else "idle")
                return
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        if metrics_server is not None:
            metrics_server.shutdown()
            metrics_server.server_close()
        telemetry.shutdown()
        for engine in engines:
            engine.dispose()


@app.command("reconcile-deletions-once")
def reconcile_deletions_once() -> None:
    """Reconcile every externally prepared deletion once."""

    coordinator, engine = _configured_deletion_reconciler()
    try:
        result = coordinator.reconcile(now=datetime.now(UTC))
        typer.echo(
            f"advanced={result.advanced} quarantined={result.quarantined} "
            f"examined={result.prepared_examined + result.controls_examined}"
        )
    finally:
        engine.dispose()


@app.command("reconcile-deletions")
def reconcile_deletions() -> None:
    """Reconcile externally prepared deletions at startup and periodically."""

    coordinator, engine = _configured_deletion_reconciler()
    try:
        while True:
            coordinator.reconcile(now=datetime.now(UTC))
            time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        engine.dispose()


@app.command("run-deletion-once")
def run_deletion_once() -> None:
    """Lease and advance at most one durable account deletion."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_deletion_worker(telemetry)
    try:
        processed = _run_worker_poll(telemetry, "DELETION", worker)
        typer.echo("processed" if processed else "idle")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-deletions")
def run_deletions() -> None:
    """Poll continuously for durable account deletions."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_deletion_worker(telemetry)
    try:
        while True:
            processed = _run_worker_poll(telemetry, "DELETION", worker)
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("schedule-retention-once")
def schedule_retention_once() -> None:
    """Create the canonical system retention job for the current UTC hour."""

    scheduler, engine = _configured_retention_scheduler()
    try:
        now = datetime.now(UTC)
        submission = scheduler.schedule(now=now)
        raw_deadlines = scheduler.schedule_raw_expirations(now=now)
        typer.echo(
            f"job_id={submission.job_id} repeated={str(submission.repeated).lower()} "
            f"scheduled_for={submission.scheduled_for.isoformat()} "
            f"raw_deadline_jobs={len(raw_deadlines)}"
        )
    finally:
        engine.dispose()


@app.command("schedule-retention")
def schedule_retention() -> None:
    """Continuously ensure each hourly system retention job exists."""

    scheduler, engine = _configured_retention_scheduler()
    try:
        while True:
            now = datetime.now(UTC)
            scheduler.schedule(now=now)
            scheduler.schedule_raw_expirations(now=now)
            time.sleep(RETENTION_SCHEDULER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        engine.dispose()


@app.command("run-retention-once")
def run_retention_once() -> None:
    """Lease and execute at most one durable system retention job."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_retention_worker(telemetry)
    try:
        processed = _run_worker_poll(telemetry, "RETENTION", worker)
        typer.echo("processed" if processed else "idle")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-retention")
def run_retention() -> None:
    """Poll continuously for durable system retention jobs."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_retention_worker(telemetry)
    try:
        while True:
            processed = _run_worker_poll(telemetry, "RETENTION", worker)
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("create-backup")
def create_backup() -> None:
    """Create and read-after-write verify one encrypted backup."""

    try:
        result = _configured_backup_service().create(now=datetime.now(UTC))
    except (BackupError, ObjectStoreError, RuntimeError) as error:
        code = getattr(error, "code", "BACKUP_CONFIGURATION_INVALID")
        typer.echo(f"{code}: backup creation failed", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"backup_id={result.backup_id} expires_at={result.expires_at.isoformat()} "
        f"objects={result.object_count} plaintext_bytes={result.total_plaintext_bytes} "
        f"database_sha256={result.database_content_hash} "
        f"manifest_sha256={result.manifest_content_hash}"
    )


@app.command("verify-backup")
def verify_backup(backup_id: str) -> None:
    """Verify one encrypted backup against its committed manifest."""

    try:
        result = _configured_backup_service().verify(backup_id)
    except (BackupError, ObjectStoreError, RuntimeError) as error:
        code = getattr(error, "code", "BACKUP_CONFIGURATION_INVALID")
        typer.echo(f"{code}: backup verification failed", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"backup_id={result.backup_id} verified=true "
        f"expires_at={result.expires_at.isoformat()} "
        f"manifest_sha256={result.manifest_content_hash}"
    )


@app.command("expire-backups-once")
def expire_backups_once() -> None:
    """Apply the fixed 30-day encrypted-backup retention deadline once."""

    try:
        primary_configuration = _object_store_configuration()
        runtime = _backup_runtime_configuration(primary_configuration)
        outcome = BackupRetentionService(
            runtime.store.build(
                ensure_bucket=os.getenv("RATEREPLAY_ENV", "development") == "development"
            )
        ).expire(now=datetime.now(UTC))
    except (BackupError, ObjectStoreError, RuntimeError) as error:
        code = getattr(error, "code", "BACKUP_CONFIGURATION_INVALID")
        typer.echo(f"{code}: backup retention failed", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"expired_backups={outcome.expired_backups} deleted_objects={outcome.deleted_objects}"
    )


@app.command("qualify-restore")
def qualify_restore(
    artifact_file: Annotated[
        Path,
        typer.Option(
            "--artifact-file",
            file_okay=True,
            dir_okay=False,
            writable=True,
            resolve_path=True,
            help="Private path for the complete restore qualification artifact.",
        ),
    ],
    outcome_evidence_file: Annotated[
        Path | None,
        typer.Option(
            "--outcome-evidence-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Optional signed authoritative outcomes for unresolved preparations.",
        ),
    ] = None,
) -> None:
    """Suppress restored data and fail closed until network exposure is safe."""

    engine: Engine | None = None
    try:
        reconciler, engine = _configured_restore_reconciler()
        qualification = reconciler.qualify(
            now=datetime.now(UTC),
            outcome_evidence=_read_outcome_evidence(outcome_evidence_file),
        )
        write_restore_qualification_artifact(artifact_file, qualification)
        verified = verify_restore_qualification_artifact(
            json.loads(artifact_file.read_text(encoding="ascii"))
        )
        typer.echo(
            f"exposure_allowed={str(verified.exposure_allowed).lower()} "
            f"holds={len(verified.quarantine_holds)} "
            f"artifact_sha256={verified.artifact_sha256}"
        )
        if not verified.exposure_allowed:
            raise typer.Exit(code=3)
    except (DeletionLedgerError, RestoreQualificationError) as error:
        typer.echo(f"{error.code}: {error}", err=True)
        raise typer.Exit(code=1) from error
    finally:
        if engine is not None:
            engine.dispose()


@app.command("restore-backup-to-quarantine")
def restore_backup_to_quarantine(
    backup_id: Annotated[str, typer.Argument(help="Committed backup identifier to restore.")],
    materialization_directory: Annotated[
        Path,
        typer.Option(
            "--materialization-directory",
            file_okay=False,
            dir_okay=True,
            writable=True,
            resolve_path=True,
            help="New private directory for verified backup materialization.",
        ),
    ],
    qualification_artifact_file: Annotated[
        Path,
        typer.Option(
            "--qualification-artifact-file",
            file_okay=True,
            dir_okay=False,
            writable=True,
            resolve_path=True,
            help="Private path for the restore qualification artifact.",
        ),
    ],
    exposure_artifact_file: Annotated[
        Path,
        typer.Option(
            "--exposure-artifact-file",
            file_okay=True,
            dir_okay=False,
            writable=True,
            resolve_path=True,
            help="Private path for the instance-bound exposure artifact.",
        ),
    ],
    outcome_evidence_file: Annotated[
        Path | None,
        typer.Option(
            "--outcome-evidence-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Optional signed authoritative outcomes for unresolved preparations.",
        ),
    ] = None,
) -> None:
    """Restore one verified backup into quarantine and bind its exposure decision."""

    engine: Engine | None = None
    try:
        backups = _configured_backup_service()
        materialized = backups.materialize_verified(backup_id, materialization_directory)
        restored_database_hash = _configured_postgres_restore_runner().restore(
            materialized.database_dump_path
        )
        if restored_database_hash != materialized.database_content_hash:
            raise BackupError(
                "PG_RESTORE_INPUT_CHANGED",
                "PostgreSQL restore input changed after backup verification",
            )
        backups.restore_materialized_objects(materialized, _configured_object_store())
        reconciler, engine = _configured_restore_reconciler()
        database_revision = _restored_database_revision(engine)
        qualification = reconciler.qualify(
            now=datetime.now(UTC),
            outcome_evidence=_read_outcome_evidence(outcome_evidence_file),
        )
        write_restore_qualification_artifact(qualification_artifact_file, qualification)
        verified_qualification = verify_restore_qualification_artifact(
            json.loads(qualification_artifact_file.read_text(encoding="ascii"))
        )
        exposure = bind_restore_exposure(
            materialized,
            verified_qualification,
            deletion_ledger_root=Path(
                os.getenv(
                    "RATEREPLAY_DELETION_LEDGER_ROOT",
                    "/var/lib/ratereplay/deletion-ledger",
                )
            ),
            database_revision=database_revision,
            bound_at=datetime.now(UTC),
        )
        write_restore_exposure_artifact(exposure_artifact_file, exposure)
        verified_exposure = verify_restore_exposure_artifact(
            json.loads(exposure_artifact_file.read_text(encoding="ascii"))
        )
        typer.echo(
            f"backup_id={verified_exposure.backup_id} "
            f"restore_instance_id={verified_exposure.restore_instance_id} "
            f"exposure_allowed={str(verified_exposure.exposure_allowed).lower()} "
            f"qualification_sha256={verified_exposure.qualification_artifact_sha256} "
            f"artifact_sha256={verified_exposure.artifact_sha256}"
        )
        if not verified_exposure.exposure_allowed:
            raise typer.Exit(code=3)
    except typer.Exit:
        raise
    except (
        BackupError,
        DeletionLedgerError,
        ObjectStoreError,
        RestoreQualificationError,
        RuntimeError,
    ) as error:
        code = getattr(error, "code", "RESTORE_CONFIGURATION_INVALID")
        typer.echo(f"{code}: quarantine restore failed", err=True)
        raise typer.Exit(code=1) from error
    finally:
        if engine is not None:
            engine.dispose()


@app.command("rotate-deletion-keys")
def rotate_deletion_keys(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            writable=True,
            resolve_path=True,
        ),
    ],
    keys_dir: Annotated[
        Path,
        typer.Option(
            "--keys-dir",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    restore_keys_dir: Annotated[
        Path,
        typer.Option(
            "--restore-keys-dir",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    expected_ledger_key_version: Annotated[str, typer.Option()],
    new_ledger_key_version: Annotated[str, typer.Option()],
    expected_restore_key_version: Annotated[str, typer.Option()],
    new_restore_key_version: Annotated[str, typer.Option()],
    expected_head_sha256: Annotated[str, typer.Option()],
    artifact_file: Annotated[
        Path,
        typer.Option(
            "--artifact-file",
            file_okay=True,
            dir_okay=False,
            writable=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """Rotate pre-staged deletion ledger and restore keys without rewriting history."""

    try:
        ledger_keyring = load_keyring(keys_dir, current_version=new_ledger_key_version)
        restore_keyring = load_keyring(
            restore_keys_dir,
            current_version=new_restore_key_version,
        )
        artifact = FilesystemDeletionLedger.rotate_keys(
            root,
            ledger_keyring=ledger_keyring,
            restore_keyring=restore_keyring,
            expected_ledger_key_version=expected_ledger_key_version,
            expected_restore_key_version=expected_restore_key_version,
            expected_head_sha256=expected_head_sha256,
            rotated_at=datetime.now(UTC),
        )
        write_rotation_artifact(artifact_file, artifact)
        verified = verify_rotation_artifact(json.loads(artifact_file.read_text(encoding="ascii")))
    except (
        DeletionLedgerError,
        KeyringError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        code = getattr(error, "code", "KEY_ROTATION_FAILED")
        typer.echo(f"{code}: deletion key rotation failed", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"rotation_sequence={verified.rotation_sequence} "
        f"ledger_key_version={verified.current_ledger_key_version} "
        f"restore_key_version={verified.current_restore_key_version} "
        f"artifact_sha256={verified.artifact_sha256}"
    )


@app.command("migrate-deletion-ledger-v1")
def migrate_deletion_ledger_v1(
    source_root: Annotated[
        Path,
        typer.Option(
            "--source-root",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    destination_root: Annotated[
        Path,
        typer.Option(
            "--destination-root",
            file_okay=False,
            dir_okay=True,
            writable=True,
            resolve_path=True,
        ),
    ],
    legacy_integrity_key_file: Annotated[
        Path,
        typer.Option(
            "--legacy-integrity-key-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    ledger_keys_dir: Annotated[
        Path,
        typer.Option(
            "--ledger-keys-dir",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    ledger_current_key_version: Annotated[str, typer.Option()],
    restore_keys_dir: Annotated[
        Path,
        typer.Option(
            "--restore-keys-dir",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    restore_current_key_version: Annotated[str, typer.Option()],
    artifact_file: Annotated[
        Path,
        typer.Option(
            "--artifact-file",
            file_okay=True,
            dir_okay=False,
            writable=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """Migrate a locked plaintext v1 ledger into a separate encrypted v2 directory."""

    try:
        legacy_key = legacy_integrity_key_file.read_bytes().strip()
        ledger_keyring = load_keyring(
            ledger_keys_dir,
            current_version=ledger_current_key_version,
        )
        restore_keyring = load_keyring(
            restore_keys_dir,
            current_version=restore_current_key_version,
        )
        artifact = migrate_plaintext_v1_ledger(
            source_root,
            destination_root,
            legacy_integrity_key=legacy_key,
            ledger_keyring=ledger_keyring,
            restore_keyring=restore_keyring,
            migrated_at=datetime.now(UTC),
        )
        write_migration_artifact(artifact_file, artifact)
        verified = verify_migration_artifact(json.loads(artifact_file.read_text(encoding="ascii")))
    except (
        DeletionLedgerError,
        KeyringError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        code = getattr(error, "code", "LEDGER_MIGRATION_FAILED")
        typer.echo(f"{code}: deletion ledger migration failed", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"migrated_events={verified.source_event_count} "
        f"ledger_key_version={verified.ledger_key_version} "
        f"restore_key_version={verified.restore_key_version} "
        f"artifact_sha256={verified.artifact_sha256}"
    )


@app.command("verify-restore-qualification")
def verify_restore_qualification(
    artifact_file: Annotated[
        Path,
        typer.Option(
            "--artifact-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """Verify an exposure-gate artifact and reject a quarantined restore."""

    try:
        payload = json.loads(artifact_file.read_text(encoding="ascii"))
        if not isinstance(payload, dict):
            raise TypeError
        verified = verify_restore_qualification_artifact(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        RestoreQualificationError,
    ) as error:
        code = getattr(error, "code", "QUALIFICATION_ARTIFACT_INVALID")
        typer.echo(f"{code}: restore qualification artifact verification failed", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"exposure_allowed={str(verified.exposure_allowed).lower()} "
        f"artifact_sha256={verified.artifact_sha256}"
    )
    if not verified.exposure_allowed:
        raise typer.Exit(code=3)


@app.command("verify-restore-exposure")
def verify_restore_exposure(
    artifact_file: Annotated[
        Path,
        typer.Option(
            "--artifact-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """Verify an instance-bound exposure artifact and reject a quarantined restore."""

    try:
        payload = json.loads(artifact_file.read_text(encoding="ascii"))
        if not isinstance(payload, dict):
            raise TypeError
        verified = verify_restore_exposure_artifact(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        RestoreQualificationError,
    ) as error:
        code = getattr(error, "code", "RESTORE_EXPOSURE_ARTIFACT_INVALID")
        typer.echo(f"{code}: restore exposure artifact verification failed", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"backup_id={verified.backup_id} "
        f"restore_instance_id={verified.restore_instance_id} "
        f"exposure_allowed={str(verified.exposure_allowed).lower()} "
        f"artifact_sha256={verified.artifact_sha256}"
    )
    if not verified.exposure_allowed:
        raise typer.Exit(code=3)


@app.command("run-report-once")
def run_report_once() -> None:
    """Lease and publish at most one redacted report export."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_report_worker(telemetry)
    try:
        processed = _run_worker_poll(telemetry, "REPORT", worker)
        typer.echo("processed" if processed else "idle")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-reports")
def run_reports() -> None:
    """Poll continuously for durable redacted report jobs."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_report_worker(telemetry)
    try:
        while True:
            processed = _run_worker_poll(telemetry, "REPORT", worker)
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-replay-once")
def run_replay_once() -> None:
    """Lease and publish at most one durable historical replay."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_replay_worker(telemetry)
    try:
        processed = _run_worker_poll(telemetry, "REPLAY", worker)
        typer.echo("processed" if processed else "idle")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-replays")
def run_replays() -> None:
    """Poll continuously for durable historical replay jobs."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_replay_worker(telemetry)
    try:
        while True:
            processed = _run_worker_poll(telemetry, "REPLAY", worker)
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-comparison-once")
def run_comparison_once() -> None:
    """Lease and publish at most one durable tariff comparison."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_comparison_worker(telemetry)
    try:
        processed = _run_worker_poll(telemetry, "COMPARISON", worker)
        typer.echo("processed" if processed else "idle")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-comparisons")
def run_comparisons() -> None:
    """Poll continuously for durable tariff comparison jobs."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_comparison_worker(telemetry)
    try:
        while True:
            processed = _run_worker_poll(telemetry, "COMPARISON", worker)
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-scenario-once")
def run_scenario_once() -> None:
    """Lease and publish at most one durable flexible-load scenario."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_scenario_worker(telemetry)
    try:
        processed = _run_worker_poll(telemetry, "SCENARIO", worker)
        typer.echo("processed" if processed else "idle")
    finally:
        telemetry.shutdown()
        engine.dispose()


@app.command("run-scenarios")
def run_scenarios() -> None:
    """Poll continuously for durable flexible-load scenario jobs."""

    telemetry = _configured_telemetry()
    worker, engine = _configured_scenario_worker(telemetry)
    try:
        while True:
            processed = _run_worker_poll(telemetry, "SCENARIO", worker)
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        telemetry.shutdown()
        engine.dispose()


if __name__ == "__main__":
    app()
