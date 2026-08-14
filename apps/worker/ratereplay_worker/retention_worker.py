"""Fenced worker for deterministic system-scope retention jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.deletion_ledger import DeletionLedgerError
from ratereplay_persistence.deletion_sweep import DeletionSweepError, DeletionSweepService
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import JobRecord
from ratereplay_persistence.object_store import ObjectStoreError
from ratereplay_persistence.retention import (
    DatabaseRetentionOutcome,
    DatabaseRetentionService,
    RetentionError,
    validate_retention_job,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    raw_objects: int
    orphan_artifacts: int
    receipt_verifiers: int
    database: DatabaseRetentionOutcome


class RetentionWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        session_factory: sessionmaker[Session],
        jobs: JobService,
        imports: ImportService,
        artifacts: ArtifactService,
        deletions: DeletionSweepService,
        database_retention: DatabaseRetentionService,
    ) -> None:
        self._worker_id = worker_id
        self._sessions = session_factory
        self._jobs = jobs
        self._imports = imports
        self._artifacts = artifacts
        self._deletions = deletions
        self._database = database_retention
        self.last_outcome: RetentionOutcome | None = None

    def run_once(self, *, now: datetime) -> bool:
        now = now.astimezone(UTC)
        lease = self._jobs.lease_next(
            worker_id=self._worker_id,
            now=now,
            kinds=frozenset({"RETENTION"}),
        )
        if lease is None:
            return False
        if not self._jobs.start(lease, now=now):
            return True
        self.last_outcome = None
        try:
            with self._sessions() as database:
                job = database.get(JobRecord, lease.job_id)
                if job is None:
                    raise RetentionError(
                        "RETENTION_JOB_INVALID",
                        "Retention job is unavailable",
                    )
                request = validate_retention_job(job)
            outcome = RetentionOutcome(
                raw_objects=self._imports.expire_raw_objects(now=now),
                orphan_artifacts=self._artifacts.sweep_orphans(
                    now=now,
                    older_than=now - timedelta(seconds=request.orphan_grace_seconds),
                ),
                receipt_verifiers=self._deletions.expire_receipt_verifiers(now=now),
                database=self._database.expire(current_job_id=lease.job_id, now=now),
            )
            if not self._jobs.complete_system(lease, now=now):
                raise RetentionError(
                    "STALE_RETENTION_LEASE",
                    "Retention job lost its completion fence",
                )
            self.last_outcome = outcome
        except ObjectStoreError as error:
            self._jobs.fail(lease, code=error.code, retryable=True, now=now)
        except SQLAlchemyError:
            self._jobs.fail(
                lease,
                code="RETENTION_DATABASE_FAILED",
                retryable=True,
                now=now,
            )
        except (DeletionLedgerError, DeletionSweepError, RetentionError) as error:
            self._jobs.fail(lease, code=error.code, retryable=False, now=now)
        return True
