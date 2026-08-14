from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_ingestion.espi import parse_espi
from ratereplay_ingestion.normalize import normalize_espi
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.imports import ImportService, ImportServiceError
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    RawObjectRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore, ObjectStoreError
from ratereplay_worker.import_worker import ImportWorker
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
ESPI_FIXTURE = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"
ESPI_SCHEMA = ROOT / "third_party/espi-schema/espi-4.0.xsd"
NOW = datetime(2026, 8, 13, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Harness:
    sessions: sessionmaker[Session]
    imports: ImportService
    jobs: JobService
    worker: ImportWorker
    objects: FilesystemObjectStore
    user_id: str


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    user_id = "a" * 32
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=user_id,
                username_canonical="import_owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
    objects = FilesystemObjectStore(tmp_path / "objects")
    imports = ImportService(sessions, objects)
    jobs = JobService(sessions)
    return Harness(
        sessions=sessions,
        imports=imports,
        jobs=jobs,
        worker=ImportWorker(
            worker_id="worker-one",
            jobs=jobs,
            imports=imports,
            espi_schema_path=ESPI_SCHEMA,
        ),
        objects=objects,
        user_id=user_id,
    )


def submit(harness: Harness, *, key: str = "import-key-one"):  # type: ignore[no-untyped-def]
    return harness.imports.submit(
        owner_user_id=harness.user_id,
        adapter="ESPI_XML",
        idempotency_key=key,
        source=BytesIO(ESPI_FIXTURE.read_bytes()),
        now=NOW,
    )


def _counts(harness: Harness, import_id: str) -> tuple[int, int]:
    with harness.sessions() as database:
        readings = database.scalar(
            select(func.count())
            .select_from(ImportReadingRecord)
            .where(ImportReadingRecord.import_id == import_id)
        )
        attempts = database.scalar(
            select(func.count())
            .select_from(JobAttemptRecord)
            .join(JobRecord)
            .where(JobRecord.import_id == import_id)
        )
        return int(readings or 0), int(attempts or 0)


def test_submission_worker_confirmation_and_raw_deletion(harness: Harness) -> None:
    submission = submit(harness)
    assert harness.worker.run_once(now=NOW)
    draft = harness.imports.draft(
        owner_user_id=harness.user_id,
        import_id=submission.import_id,
    )
    start = draft.readings[0].start_utc_ns
    end = draft.readings[-1].start_utc_ns + draft.readings[-1].duration_seconds * 1_000_000_000
    profile = harness.imports.confirm(
        owner_user_id=harness.user_id,
        import_id=submission.import_id,
        billing_period_start_utc_ns=start,
        billing_period_end_utc_ns=end,
        acknowledged_warning_ids=draft.warning_ids,
        pge_service_attested=True,
        now=NOW,
    )
    assert profile.content_hash
    assert len(profile.canonical_content) > 100
    assert _counts(harness, submission.import_id) == (362, 1)
    with harness.sessions() as database:
        imported = database.get(ImportRecord, submission.import_id)
        raw = database.scalar(
            select(RawObjectRecord).where(RawObjectRecord.import_id == submission.import_id)
        )
        job = database.get(JobRecord, submission.job_id)
        assert imported is not None and imported.state == "CONFIRMED"
        assert raw is not None and raw.state == "DELETED"
        assert not harness.objects.exists(raw.object_key)
        assert job is not None and job.state == "SUCCEEDED"


def test_duplicate_submission_reuses_operation_and_conflicting_payload_fails(
    harness: Harness,
) -> None:
    first = submit(harness)
    repeated = submit(harness)
    assert repeated.import_id == first.import_id
    assert repeated.job_id == first.job_id
    assert repeated.repeated
    with pytest.raises(ImportServiceError) as raised:
        harness.imports.submit(
            owner_user_id=harness.user_id,
            adapter="ESPI_XML",
            idempotency_key="import-key-one",
            source=BytesIO(ESPI_FIXTURE.read_bytes() + b"\n"),
            now=NOW,
        )
    assert raised.value.code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize("crash_point", ["after_lease", "during_parse", "after_parse"])
def test_worker_crash_recovers_without_duplicate_draft(harness: Harness, crash_point: str) -> None:
    submission = submit(harness)

    def crash(_lease: JobLease) -> None:
        raise RuntimeError("injected worker termination")

    arguments = {crash_point: crash}
    with pytest.raises(RuntimeError, match="injected worker termination"):
        harness.worker.run_once(now=NOW, **arguments)
    assert harness.jobs.rescue_expired(now=NOW + timedelta(seconds=21)) == 1
    assert harness.worker.run_once(now=NOW + timedelta(seconds=21))
    assert _counts(harness, submission.import_id) == (362, 2)


def test_crash_after_atomic_publication_leaves_one_terminal_result(harness: Harness) -> None:
    submission = submit(harness)

    def crash(_lease: JobLease) -> None:
        raise RuntimeError("response lost after publication")

    with pytest.raises(RuntimeError, match="response lost"):
        harness.worker.run_once(now=NOW, after_publish=crash)
    assert not harness.worker.run_once(now=NOW + timedelta(seconds=21))
    assert _counts(harness, submission.import_id) == (362, 1)


def test_stale_attempt_cannot_publish_after_replacement_lease(harness: Harness) -> None:
    submission = submit(harness)
    first = harness.jobs.lease_next(worker_id="worker-old", now=NOW)
    assert first is not None
    assert harness.jobs.start(first, now=NOW)
    second_now = NOW + timedelta(seconds=21)
    second = harness.jobs.lease_next(worker_id="worker-new", now=second_now)
    assert second is not None
    assert second.fencing_generation > first.fencing_generation
    assert harness.jobs.start(second, now=second_now)
    draft = normalize_espi(parse_espi(ESPI_FIXTURE.read_bytes(), schema_path=ESPI_SCHEMA))
    assert not harness.imports.publish_draft(
        import_id=submission.import_id,
        draft=draft,
        worker_id=first.worker_id,
        fencing_generation=first.fencing_generation,
        now=second_now,
    )
    assert harness.imports.publish_draft(
        import_id=submission.import_id,
        draft=draft,
        worker_id=second.worker_id,
        fencing_generation=second.fencing_generation,
        now=second_now,
    )
    assert _counts(harness, submission.import_id) == (362, 2)


def test_lifecycle_generation_fences_heartbeat_and_finalization(harness: Harness) -> None:
    submission = submit(harness)
    lease = harness.jobs.lease_next(worker_id="worker-old", now=NOW)
    assert lease is not None and harness.jobs.start(lease, now=NOW)
    with harness.sessions.begin() as database:
        imported = database.get(ImportRecord, submission.import_id)
        assert imported is not None
        imported.lifecycle_generation += 1
    assert not harness.jobs.heartbeat(lease, now=NOW + timedelta(seconds=1))
    draft = normalize_espi(parse_espi(ESPI_FIXTURE.read_bytes(), schema_path=ESPI_SCHEMA))
    assert not harness.imports.publish_draft(
        import_id=submission.import_id,
        draft=draft,
        worker_id=lease.worker_id,
        fencing_generation=lease.fencing_generation,
        now=NOW,
    )


def test_raw_retention_removes_expired_object(harness: Harness) -> None:
    submission = submit(harness)
    with harness.sessions() as database:
        raw = database.scalar(
            select(RawObjectRecord).where(RawObjectRecord.import_id == submission.import_id)
        )
        assert raw is not None and harness.objects.exists(raw.object_key)
        key = raw.object_key
    assert harness.imports.expire_raw_objects(now=NOW + timedelta(hours=24)) == 1
    assert not harness.objects.exists(key)


def test_failed_raw_deletion_remains_pending_and_retention_retries(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submission = submit(harness)
    assert harness.worker.run_once(now=NOW)
    draft = harness.imports.draft(
        owner_user_id=harness.user_id,
        import_id=submission.import_id,
    )
    start = draft.readings[0].start_utc_ns
    end = draft.readings[-1].start_utc_ns + draft.readings[-1].duration_seconds * 1_000_000_000
    real_delete = harness.objects.delete

    def fail_delete(_key: str) -> None:
        raise ObjectStoreError("OBJECT_DELETE_FAILED", "Raw object deletion failed")

    monkeypatch.setattr(harness.objects, "delete", fail_delete)
    with pytest.raises(ObjectStoreError, match="deletion failed"):
        harness.imports.confirm(
            owner_user_id=harness.user_id,
            import_id=submission.import_id,
            billing_period_start_utc_ns=start,
            billing_period_end_utc_ns=end,
            acknowledged_warning_ids=draft.warning_ids,
            pge_service_attested=True,
            now=NOW,
        )
    with harness.sessions() as database:
        raw = database.scalar(
            select(RawObjectRecord).where(RawObjectRecord.import_id == submission.import_id)
        )
        assert raw is not None and raw.state == "DELETE_PENDING"
        key = raw.object_key
        assert harness.objects.exists(key)
    monkeypatch.setattr(harness.objects, "delete", real_delete)
    assert harness.imports.expire_raw_objects(now=NOW + timedelta(minutes=1)) == 1
    assert not harness.objects.exists(key)
    with harness.sessions() as database:
        raw = database.scalar(
            select(RawObjectRecord).where(RawObjectRecord.import_id == submission.import_id)
        )
        assert raw is not None and raw.state == "DELETED"


def test_persisted_readings_and_profiles_are_application_immutable(harness: Harness) -> None:
    submission = submit(harness)
    assert harness.worker.run_once(now=NOW)
    with harness.sessions() as database:
        reading = database.scalar(
            select(ImportReadingRecord).where(ImportReadingRecord.import_id == submission.import_id)
        )
        assert reading is not None
        reading.energy_wh += 1
        with pytest.raises(RuntimeError, match="immutable"):
            database.commit()


def test_owner_scope_blocks_cross_account_draft_access(harness: Harness) -> None:
    submission = submit(harness)
    assert harness.worker.run_once(now=NOW)
    with pytest.raises(ImportServiceError) as raised:
        harness.imports.draft(owner_user_id="b" * 32, import_id=submission.import_id)
    assert raised.value.code == "IMPORT_NOT_FOUND"


def test_locked_pge_csv_runs_through_durable_worker(harness: Harness) -> None:
    submission = harness.imports.submit(
        owner_user_id=harness.user_id,
        adapter="PGE_CSV",
        idempotency_key="provider-csv-import",
        source=BytesIO((ROOT / "third_party/pge-csv/provider-sample.csv").read_bytes()),
        now=NOW,
    )
    assert harness.worker.run_once(now=NOW)
    assert _counts(harness, submission.import_id) == (5_664, 1)


def test_retry_budget_is_bounded_and_exhaustion_is_terminal(harness: Harness) -> None:
    submission = submit(harness)
    current = NOW
    for attempt_number in range(1, 4):
        lease = harness.jobs.lease_next(worker_id=f"worker-{attempt_number}", now=current)
        assert lease is not None
        assert harness.jobs.start(lease, now=current)
        assert harness.jobs.fail(
            lease,
            code="TRANSIENT_IMPORT_STORAGE_FAILURE",
            retryable=True,
            now=current,
        )
        current += timedelta(minutes=10)
    assert harness.jobs.lease_next(worker_id="worker-four", now=current) is None
    with harness.sessions() as database:
        job = database.get(JobRecord, submission.job_id)
        imported = database.get(ImportRecord, submission.import_id)
        assert job is not None and job.state == "FAILED"
        assert job.failure_code == "ATTEMPT_BUDGET_EXHAUSTED"
        assert imported is not None and imported.state == "FAILED"
    assert _counts(harness, submission.import_id) == (0, 3)


def test_cancellation_before_lease_prevents_execution(harness: Harness) -> None:
    submission = submit(harness)
    assert harness.jobs.cancel(
        owner_user_id=harness.user_id,
        job_id=submission.job_id,
        now=NOW,
    )
    assert harness.jobs.lease_next(worker_id="worker", now=NOW) is None
    with harness.sessions() as database:
        job = database.get(JobRecord, submission.job_id)
        assert job is not None and job.state == "CANCELLED"


def test_heartbeat_extends_only_the_current_fenced_lease(harness: Harness) -> None:
    submit(harness)
    lease = harness.jobs.lease_next(worker_id="worker", now=NOW)
    assert lease is not None and harness.jobs.start(lease, now=NOW)
    assert harness.jobs.heartbeat(lease, now=NOW + timedelta(seconds=10))
    stale = JobLease(
        job_id=lease.job_id,
        import_id=lease.import_id,
        worker_id=lease.worker_id,
        attempt_number=lease.attempt_number,
        fencing_generation=lease.fencing_generation - 1,
        lease_expires_at=lease.lease_expires_at,
    )
    assert not harness.jobs.heartbeat(stale, now=NOW + timedelta(seconds=11))


def test_permanent_parser_failure_never_retries(harness: Harness) -> None:
    submission = harness.imports.submit(
        owner_user_id=harness.user_id,
        adapter="ESPI_XML",
        idempotency_key="malformed-import",
        source=BytesIO(b"not xml"),
        now=NOW,
    )
    assert harness.worker.run_once(now=NOW)
    assert not harness.worker.run_once(now=NOW + timedelta(minutes=10))
    with harness.sessions() as database:
        job = database.get(JobRecord, submission.job_id)
        imported = database.get(ImportRecord, submission.import_id)
        assert job is not None and job.state == "FAILED"
        assert job.failure_code == "XML_SCHEMA_FAILURE"
        assert imported is not None and imported.failure_code == "XML_SCHEMA_FAILURE"
    assert _counts(harness, submission.import_id) == (0, 1)
