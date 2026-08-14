"""Public durable-worker command line entry point."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from ratereplay_domain.environment import environment_lock_hash
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import DeletionLedgerError, FilesystemDeletionLedger
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.deletions import DeletionCoordinator
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.restore import (
    RestoreQualificationError,
    RestoreReconciler,
    TransactionOutcomeEvidence,
    verify_restore_qualification_artifact,
    write_restore_qualification_artifact,
)
from ratereplay_persistence.retention import DatabaseRetentionService, RetentionScheduler
from ratereplay_tariffs.admission import load_all_admitted_tariffs
from ratereplay_tariffs.comparison import load_required_component_keys
from sqlalchemy.engine import Engine

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


@app.callback()
def main() -> None:
    """Run durable RateReplay worker operations."""


def _configured_worker() -> tuple[ImportWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    schema_path = Path(
        os.getenv(
            "RATEREPLAY_ESPI_SCHEMA_PATH",
            "third_party/espi-schema/espi-4.0.xsd",
        )
    )
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    imports = ImportService(sessions, FilesystemObjectStore(object_root))
    worker = ImportWorker(
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
        jobs=JobService(sessions),
        imports=imports,
        espi_schema_path=schema_path,
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
    ledger_key = _required_key_file("RATEREPLAY_DELETION_LEDGER_KEY_FILE")
    restore_key = _required_key_file("RATEREPLAY_RESTORE_KEY_FILE")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    coordinator = DeletionCoordinator(
        sessions,
        FilesystemDeletionLedger(ledger_root, integrity_key=ledger_key),
        restore_key=restore_key,
        restore_key_version=os.getenv("RATEREPLAY_RESTORE_KEY_VERSION", "restore-v1"),
    )
    return coordinator, engine


def _configured_deletion_worker() -> tuple[DeletionWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    ledger_root = Path(
        os.getenv("RATEREPLAY_DELETION_LEDGER_ROOT", "/var/lib/ratereplay/deletion-ledger")
    )
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    ledger_key = _required_key_file("RATEREPLAY_DELETION_LEDGER_KEY_FILE")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    jobs = JobService(sessions)
    ledger = FilesystemDeletionLedger(ledger_root, integrity_key=ledger_key)
    worker = DeletionWorker(
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
        jobs=jobs,
        sweeps=DeletionSweepService(
            sessions,
            FilesystemObjectStore(object_root),
            ledger,
        ),
    )
    return worker, engine


def _configured_retention_scheduler() -> tuple[RetentionScheduler, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    engine = make_engine(database_url)
    return RetentionScheduler(make_session_factory(engine)), engine


def _configured_retention_worker() -> tuple[RetentionWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    ledger_root = Path(
        os.getenv("RATEREPLAY_DELETION_LEDGER_ROOT", "/var/lib/ratereplay/deletion-ledger")
    )
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    ledger_key = _required_key_file("RATEREPLAY_DELETION_LEDGER_KEY_FILE")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(object_root)
    ledger = FilesystemDeletionLedger(ledger_root, integrity_key=ledger_key)
    return (
        RetentionWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            imports=ImportService(sessions, objects),
            artifacts=ArtifactService(sessions, objects),
            deletions=DeletionSweepService(sessions, objects, ledger),
            database_retention=DatabaseRetentionService(sessions, ledger),
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
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    ledger_key = _required_key_file("RATEREPLAY_DELETION_LEDGER_KEY_FILE")
    restore_key = _required_key_file("RATEREPLAY_RESTORE_KEY_FILE")
    outcome_key = _required_key_file("RATEREPLAY_TRANSACTION_OUTCOME_KEY_FILE")
    ledger = FilesystemDeletionLedger(
        ledger_root,
        integrity_key=ledger_key,
        require_existing=True,
    )
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    return (
        RestoreReconciler(
            sessions,
            FilesystemObjectStore(object_root),
            ledger,
            restore_key=restore_key,
            restore_key_version=os.getenv("RATEREPLAY_RESTORE_KEY_VERSION", "restore-v1"),
            outcome_evidence_key=outcome_key,
        ),
        engine,
    )


def _configured_report_worker() -> tuple[ReportWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(object_root)
    return (
        ReportWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, objects),
        ),
        engine,
    )


def _configured_replay_worker() -> tuple[ReplayWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    repository_root = Path(os.getenv("RATEREPLAY_REPOSITORY_ROOT", ".")).resolve()
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    tariffs = load_all_admitted_tariffs(repository_root)
    return (
        ReplayWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, FilesystemObjectStore(object_root)),
            admitted_tariffs={item.lock.tariff_version_id: item for item in tariffs},
            environment_lock_hash=environment_lock_hash(repository_root),
        ),
        engine,
    )


def _configured_comparison_worker() -> tuple[ComparisonWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    repository_root = Path(os.getenv("RATEREPLAY_REPOSITORY_ROOT", ".")).resolve()
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    tariffs = load_all_admitted_tariffs(repository_root)
    return (
        ComparisonWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, FilesystemObjectStore(object_root)),
            admitted_tariffs={item.lock.tariff_version_id: item for item in tariffs},
            required_component_keys=load_required_component_keys(repository_root),
            environment_lock_hash=environment_lock_hash(repository_root),
        ),
        engine,
    )


def _configured_scenario_worker() -> tuple[ScenarioWorker, Engine]:
    database_url = os.getenv("RATEREPLAY_DATABASE_URL")
    if database_url is None:
        typer.echo("RATEREPLAY_DATABASE_URL is required", err=True)
        raise typer.Exit(code=2)
    repository_root = Path(os.getenv("RATEREPLAY_REPOSITORY_ROOT", ".")).resolve()
    object_root = Path(os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/var/lib/ratereplay/objects"))
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    tariffs = load_all_admitted_tariffs(repository_root)
    return (
        ScenarioWorker(
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            session_factory=sessions,
            jobs=JobService(sessions),
            artifacts=ArtifactService(sessions, FilesystemObjectStore(object_root)),
            admitted_tariffs={item.lock.tariff_version_id: item for item in tariffs},
            environment_lock_hash=environment_lock_hash(repository_root),
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
    if len(value) < 32:
        typer.echo(f"{variable} must reference at least 32 bytes", err=True)
        raise typer.Exit(code=2)
    return value


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

    worker, engine = _configured_worker()
    processed = worker.run_once(now=datetime.now(UTC))
    engine.dispose()
    typer.echo("processed" if processed else "idle")


@app.command("run")
def run() -> None:
    """Poll continuously for durable import jobs."""

    worker, engine = _configured_worker()
    try:
        while True:
            processed = worker.run_once(now=datetime.now(UTC))
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
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

    worker, engine = _configured_deletion_worker()
    try:
        processed = worker.run_once(now=datetime.now(UTC))
        typer.echo("processed" if processed else "idle")
    finally:
        engine.dispose()


@app.command("run-deletions")
def run_deletions() -> None:
    """Poll continuously for durable account deletions."""

    worker, engine = _configured_deletion_worker()
    try:
        while True:
            processed = worker.run_once(now=datetime.now(UTC))
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
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

    worker, engine = _configured_retention_worker()
    try:
        processed = worker.run_once(now=datetime.now(UTC))
        typer.echo("processed" if processed else "idle")
    finally:
        engine.dispose()


@app.command("run-retention")
def run_retention() -> None:
    """Poll continuously for durable system retention jobs."""

    worker, engine = _configured_retention_worker()
    try:
        while True:
            processed = worker.run_once(now=datetime.now(UTC))
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        engine.dispose()


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


@app.command("run-report-once")
def run_report_once() -> None:
    """Lease and publish at most one redacted report export."""

    worker, engine = _configured_report_worker()
    try:
        processed = worker.run_once(now=datetime.now(UTC))
        typer.echo("processed" if processed else "idle")
    finally:
        engine.dispose()


@app.command("run-reports")
def run_reports() -> None:
    """Poll continuously for durable redacted report jobs."""

    worker, engine = _configured_report_worker()
    try:
        while True:
            processed = worker.run_once(now=datetime.now(UTC))
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        engine.dispose()


@app.command("run-replay-once")
def run_replay_once() -> None:
    """Lease and publish at most one durable historical replay."""

    worker, engine = _configured_replay_worker()
    try:
        processed = worker.run_once(now=datetime.now(UTC))
        typer.echo("processed" if processed else "idle")
    finally:
        engine.dispose()


@app.command("run-replays")
def run_replays() -> None:
    """Poll continuously for durable historical replay jobs."""

    worker, engine = _configured_replay_worker()
    try:
        while True:
            processed = worker.run_once(now=datetime.now(UTC))
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        engine.dispose()


@app.command("run-comparison-once")
def run_comparison_once() -> None:
    """Lease and publish at most one durable tariff comparison."""

    worker, engine = _configured_comparison_worker()
    try:
        processed = worker.run_once(now=datetime.now(UTC))
        typer.echo("processed" if processed else "idle")
    finally:
        engine.dispose()


@app.command("run-comparisons")
def run_comparisons() -> None:
    """Poll continuously for durable tariff comparison jobs."""

    worker, engine = _configured_comparison_worker()
    try:
        while True:
            processed = worker.run_once(now=datetime.now(UTC))
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        engine.dispose()


@app.command("run-scenario-once")
def run_scenario_once() -> None:
    """Lease and publish at most one durable flexible-load scenario."""

    worker, engine = _configured_scenario_worker()
    try:
        processed = worker.run_once(now=datetime.now(UTC))
        typer.echo("processed" if processed else "idle")
    finally:
        engine.dispose()


@app.command("run-scenarios")
def run_scenarios() -> None:
    """Poll continuously for durable flexible-load scenario jobs."""

    worker, engine = _configured_scenario_worker()
    try:
        while True:
            processed = worker.run_once(now=datetime.now(UTC))
            if not processed:
                time.sleep(WORKER_POLL_SECONDS)
    except KeyboardInterrupt:
        typer.echo("stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    app()
