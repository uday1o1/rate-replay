"""Durable import worker that publishes only through a matching lease fence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ratereplay_ingestion.espi import EspiParseError, parse_espi
from ratereplay_ingestion.normalize import normalize_espi, normalize_pge_csv
from ratereplay_ingestion.pge_csv import PgeCsvError, parse_pge_csv
from ratereplay_persistence.imports import ImportService, ImportServiceError
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.object_store import ObjectStoreError


class ImportWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        jobs: JobService,
        imports: ImportService,
        espi_schema_path: Path,
    ) -> None:
        self._worker_id = worker_id
        self._jobs = jobs
        self._imports = imports
        self._espi_schema_path = espi_schema_path

    def run_once(
        self,
        *,
        now: datetime,
        after_lease: Callable[[JobLease], None] | None = None,
        during_parse: Callable[[JobLease], None] | None = None,
        after_parse: Callable[[JobLease], None] | None = None,
        after_publish: Callable[[JobLease], None] | None = None,
    ) -> bool:
        lease = self._jobs.lease_next(worker_id=self._worker_id, now=now)
        if lease is None:
            return False
        if after_lease is not None:
            after_lease(lease)
        if not self._jobs.start(lease, now=now):
            return False
        try:
            with self._imports.open_raw(lease.import_id) as (adapter, payload):
                if adapter == "ESPI_XML":
                    draft = normalize_espi(
                        parse_espi(
                            payload,
                            schema_path=self._espi_schema_path,
                            on_chunk=(
                                None if during_parse is None else lambda _size: during_parse(lease)
                            ),
                        )
                    )
                elif adapter == "PGE_CSV":
                    draft = normalize_pge_csv(parse_pge_csv(payload))
                else:
                    raise ImportServiceError(
                        "UNSUPPORTED_ADAPTER", "Import adapter is not supported"
                    )
            if after_parse is not None:
                after_parse(lease)
            published = self._imports.publish_draft(
                import_id=lease.import_id,
                draft=draft,
                worker_id=lease.worker_id,
                fencing_generation=lease.fencing_generation,
                now=now,
            )
            if not published:
                return False
            if after_publish is not None:
                after_publish(lease)
            return True
        except (EspiParseError, PgeCsvError) as error:
            self._jobs.fail(
                lease,
                code=error.code,
                retryable=False,
                now=now,
            )
            return True
        except (ImportServiceError, ObjectStoreError):
            self._jobs.fail(
                lease,
                code="TRANSIENT_IMPORT_STORAGE_FAILURE",
                retryable=True,
                now=now,
            )
            return True
