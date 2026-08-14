from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    ImportFindingRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    RawObjectRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_worker.import_worker import ImportWorker
from sqlalchemy import delete, func, select

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"
SCHEMA = ROOT / "third_party/espi-schema/espi-4.0.xsd"


@pytest.mark.postgres
def test_migrated_postgres_durable_import_and_fenced_publication(tmp_path: Path) -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    user_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=user_id,
                username_canonical=f"pg_{secrets.token_hex(5)}",
                password_hash="test-only",
                created_at=now,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
    imports = ImportService(sessions, FilesystemObjectStore(tmp_path / "objects"))
    jobs = JobService(sessions)
    submission = imports.submit(
        owner_user_id=user_id,
        adapter="ESPI_XML",
        idempotency_key="postgres-import-key",
        source=BytesIO(FIXTURE.read_bytes()),
        now=now,
    )
    worker = ImportWorker(
        worker_id="postgres-worker",
        jobs=jobs,
        imports=imports,
        espi_schema_path=SCHEMA,
    )
    assert worker.run_once(now=now)
    with sessions() as database:
        job = database.get(JobRecord, submission.job_id)
        imported = database.get(ImportRecord, submission.import_id)
        reading_count = database.scalar(
            select(func.count())
            .select_from(ImportReadingRecord)
            .where(ImportReadingRecord.import_id == submission.import_id)
        )
        assert job is not None and job.state == "SUCCEEDED"
        assert job.fencing_generation == 1
        assert imported is not None and imported.state == "READY"
        assert reading_count == 362

    with sessions.begin() as database:
        database.execute(
            delete(ProfileVersionRecord).where(ProfileVersionRecord.owner_user_id == user_id)
        )
        database.execute(
            delete(ImportFindingRecord).where(ImportFindingRecord.import_id == submission.import_id)
        )
        database.execute(
            delete(ImportReadingRecord).where(ImportReadingRecord.import_id == submission.import_id)
        )
        database.execute(
            delete(JobAttemptRecord).where(JobAttemptRecord.job_id == submission.job_id)
        )
        database.execute(delete(JobRecord).where(JobRecord.id == submission.job_id))
        database.execute(
            delete(RawObjectRecord).where(RawObjectRecord.import_id == submission.import_id)
        )
        database.execute(
            delete(OperationRequestRecord).where(
                OperationRequestRecord.operation_id == submission.import_id
            )
        )
        database.execute(delete(ImportRecord).where(ImportRecord.id == submission.import_id))
        database.execute(delete(UserRecord).where(UserRecord.id == user_id))
    engine.dispose()
