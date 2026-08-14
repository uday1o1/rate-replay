"""Public durable-worker command line entry point."""

from __future__ import annotations

import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

import typer
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletions import DeletionCoordinator
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.object_store import FilesystemObjectStore
from sqlalchemy.engine import Engine

from ratereplay_worker.import_worker import ImportWorker

app = typer.Typer(no_args_is_help=True)
WORKER_POLL_SECONDS = 1.0


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


def _required_key_file(variable: str) -> bytes:
    path = os.getenv(variable)
    if path is None:
        typer.echo(f"{variable} is required", err=True)
        raise typer.Exit(code=2)
    value = Path(path).read_bytes().strip()
    if len(value) < 32:
        typer.echo(f"{variable} must reference at least 32 bytes", err=True)
        raise typer.Exit(code=2)
    return value


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


if __name__ == "__main__":
    app()
