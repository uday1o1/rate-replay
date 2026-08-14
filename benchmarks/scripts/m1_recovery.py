#!/usr/bin/env python3
"""Measure the frozen durable-import recovery bound and duplicate invariant."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import LEASE_DURATION, JobLease, JobService
from ratereplay_persistence.models import (
    ImportReadingRecord,
    JobAttemptRecord,
    JobRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_worker.cli import WORKER_POLL_SECONDS
from ratereplay_worker.import_worker import ImportWorker
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"
SCHEMA = ROOT / "third_party/espi-schema/espi-4.0.xsd"
CHARTER = ROOT / "benchmarks/charters/performance-v1.json"
START = datetime(2026, 8, 13, tzinfo=UTC)
CASES = (
    "before_parse",
    "during_parse",
    "before_publish",
    "after_publish",
    "before_parse",
    "during_parse",
    "before_publish",
    "after_publish",
    "during_parse",
    "before_parse",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_evidence() -> dict[str, Any]:
    charter = json.loads(CHARTER.read_text(encoding="utf-8"))
    threshold_ms = int(charter["thresholds"]["worker_recovery_maximum_ms"])
    payload = FIXTURE.read_bytes()
    results: list[dict[str, Any]] = []
    duplicate_terminal_results = 0
    duplicate_draft_rows = 0
    with TemporaryDirectory(prefix="rate-replay-m1-recovery.") as temporary:
        root = Path(temporary)
        engine = make_engine(f"sqlite+pysqlite:///{root / 'recovery.db'}")
        Base.metadata.create_all(engine)
        sessions = make_session_factory(engine)
        user_id = "f" * 32
        with sessions.begin() as database:
            database.add(
                UserRecord(
                    id=user_id,
                    username_canonical="recovery_owner",
                    password_hash=hashlib.sha256(b"benchmark-user").hexdigest(),
                    created_at=START,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                )
            )
        imports = ImportService(sessions, FilesystemObjectStore(root / "objects"))
        jobs = JobService(sessions)

        for index, crash_point in enumerate(CASES):
            case_start = START + timedelta(minutes=index)
            submission = imports.submit(
                owner_user_id=user_id,
                adapter="ESPI_XML",
                idempotency_key=f"recovery-case-{index}",
                source=BytesIO(payload),
                now=case_start,
            )
            worker = ImportWorker(
                worker_id=f"crashing-worker-{index}",
                jobs=jobs,
                imports=imports,
                espi_schema_path=SCHEMA,
            )

            def terminate(_lease: JobLease) -> None:
                raise RuntimeError("injected worker termination")

            hooks = {
                "before_parse": {"after_lease": terminate},
                "during_parse": {"during_parse": terminate},
                "before_publish": {"after_parse": terminate},
                "after_publish": {"after_publish": terminate},
            }[crash_point]
            try:
                worker.run_once(now=case_start, **hooks)
            except RuntimeError as error:
                if str(error) != "injected worker termination":
                    raise
            else:
                raise RuntimeError(f"Recovery injection did not terminate: {crash_point}")

            wall_started = time.perf_counter_ns()
            if crash_point == "after_publish":
                recovery_bound_ms = 0.0
                replacement_processed = False
            else:
                recovery_at = case_start + LEASE_DURATION
                replacement = ImportWorker(
                    worker_id=f"replacement-worker-{index}",
                    jobs=jobs,
                    imports=imports,
                    espi_schema_path=SCHEMA,
                )
                replacement_processed = replacement.run_once(now=recovery_at)
                processing_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
                recovery_bound_ms = (
                    LEASE_DURATION.total_seconds() * 1_000
                    + WORKER_POLL_SECONDS * 1_000
                    + processing_ms
                )
            with sessions() as database:
                reading_count = int(
                    database.scalar(
                        select(func.count())
                        .select_from(ImportReadingRecord)
                        .where(ImportReadingRecord.import_id == submission.import_id)
                    )
                    or 0
                )
                successful_jobs = int(
                    database.scalar(
                        select(func.count())
                        .select_from(JobRecord)
                        .where(
                            JobRecord.id == submission.job_id,
                            JobRecord.state == "SUCCEEDED",
                        )
                    )
                    or 0
                )
                attempt_count = int(
                    database.scalar(
                        select(func.count())
                        .select_from(JobAttemptRecord)
                        .where(JobAttemptRecord.job_id == submission.job_id)
                    )
                    or 0
                )
            duplicate_draft_rows += max(0, reading_count - 362)
            duplicate_terminal_results += max(0, successful_jobs - 1)
            recovered = (
                successful_jobs == 1
                and reading_count == 362
                and (replacement_processed or crash_point == "after_publish")
            )
            results.append(
                {
                    "attempt_count": attempt_count,
                    "case": crash_point,
                    "reading_count": reading_count,
                    "recovered": recovered,
                    "recovery_upper_bound_ms": round(recovery_bound_ms, 6),
                    "successful_job_rows": successful_jobs,
                }
            )
        engine.dispose()

    maximum_ms = max(float(result["recovery_upper_bound_ms"]) for result in results)
    passed = bool(
        all(result["recovered"] for result in results)
        and maximum_ms <= threshold_ms
        and duplicate_draft_rows == 0
        and duplicate_terminal_results == 0
    )
    return {
        "benchmark": "milestone-1-durable-import-recovery",
        "charter_sha256": _sha256(CHARTER),
        "charter_version": charter["charter_version"],
        "duplicate_draft_rows": duplicate_draft_rows,
        "duplicate_terminal_results": duplicate_terminal_results,
        "fixture_sha256": _sha256(FIXTURE),
        "hardware": charter["hardware"],
        "lease_seconds": LEASE_DURATION.total_seconds(),
        "maximum_recovery_upper_bound_ms": round(maximum_ms, 6),
        "passed": passed,
        "poll_seconds": WORKER_POLL_SECONDS,
        "python": platform.python_version(),
        "recovery_cases": results,
        "schema_sha256": _sha256(SCHEMA),
        "threshold_ms": threshold_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evidence/performance/m1-import-recovery.json",
    )
    arguments = parser.parse_args()
    evidence = build_evidence()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not evidence["passed"]:
        raise SystemExit("Milestone 1 recovery charter failed")
    print(
        "Milestone 1 recovery passed: "
        f"maximum {evidence['maximum_recovery_upper_bound_ms']} ms, "
        "zero duplicate results."
    )


if __name__ == "__main__":
    main()
