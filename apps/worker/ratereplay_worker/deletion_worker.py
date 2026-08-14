"""Durable deletion worker that advances only under its exact lifecycle fence."""

from __future__ import annotations

from datetime import datetime

from ratereplay_domain.telemetry import Telemetry
from ratereplay_persistence.deletion_ledger import DeletionLedgerError
from ratereplay_persistence.deletion_sweep import DeletionSweepError, DeletionSweepService
from ratereplay_persistence.deletions import DeletionServiceError
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.object_store import ObjectStoreError


class DeletionWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        jobs: JobService,
        sweeps: DeletionSweepService,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._jobs = jobs
        self._sweeps = sweeps
        self._telemetry = telemetry

    def run_once(self, *, now: datetime) -> bool:
        lease = self._jobs.lease_next(
            worker_id=self._worker_id,
            now=now,
            kinds=frozenset({"DELETION"}),
        )
        if lease is None:
            return False
        if self._telemetry is not None:
            self._telemetry.record_job_lease(
                kind=lease.kind,
                job_id=lease.job_id,
                attempt_number=lease.attempt_number,
            )
        if not self._jobs.start(lease, now=now):
            return False
        try:
            for _phase_budget in range(5):
                outcome = self._sweeps.advance(lease, now=now)
                if outcome.state == "COMPLETED":
                    if self._telemetry is not None:
                        self._telemetry.record_deletion(outcome="SUCCEEDED")
                    return True
                if outcome.state == "PENDING":
                    self._jobs.fail(
                        lease,
                        code="DELETION_DRAIN_PENDING",
                        retryable=True,
                        now=now,
                    )
                    return True
                if not self._jobs.heartbeat(lease, now=now):
                    return False
            raise DeletionSweepError(
                "DELETION_PHASE_BUDGET_EXHAUSTED",
                "Deletion phase loop exceeded its fixed transition budget",
            )
        except (
            DeletionLedgerError,
            DeletionServiceError,
            DeletionSweepError,
            ObjectStoreError,
        ) as error:
            if self._telemetry is not None:
                self._telemetry.record_deletion(outcome="FAILED")
            self._jobs.fail(
                lease,
                code=getattr(error, "code", "DELETION_TRANSIENT_FAILURE"),
                retryable=True,
                now=now,
            )
            return True
