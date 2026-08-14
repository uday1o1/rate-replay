"""Public durable-worker command line entry point."""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import typer
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.object_store import FilesystemObjectStore

from ratereplay_worker.import_worker import ImportWorker

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run durable RateReplay worker operations."""


@app.command("run-once")
def run_once() -> None:
    """Lease and process at most one durable import job."""

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
    processed = worker.run_once(now=datetime.now(UTC))
    engine.dispose()
    typer.echo("processed" if processed else "idle")


if __name__ == "__main__":
    app()
