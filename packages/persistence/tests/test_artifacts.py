from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.artifacts import ArtifactService, ArtifactServiceError
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
    ImportRecord,
    JobRecord,
    JobResultClaimRecord,
    ObjectUploadRegistrationRecord,
    ProfileVersionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore, ObjectStoreError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ArtifactHarness:
    sessions: sessionmaker[Session]
    jobs: JobService
    artifacts: ArtifactService
    objects: FilesystemObjectStore


@pytest.fixture
def harness(tmp_path: Path) -> ArtifactHarness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    objects = FilesystemObjectStore(tmp_path / "objects")
    for prefix in ("1", "2"):
        owner_id = prefix * 32
        import_id = f"{prefix}i" * 16
        profile_id = f"{prefix}p" * 16
        with sessions.begin() as database:
            database.add(
                UserRecord(
                    id=owner_id,
                    username_canonical=f"artifact_owner_{prefix}",
                    password_hash="test-only",
                    created_at=NOW,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                )
            )
            database.flush()
            database.add(
                ImportRecord(
                    id=import_id,
                    owner_user_id=owner_id,
                    state="CONFIRMED",
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    adapter="TEST_CANONICAL",
                    raw_content_hash="a" * 64,
                    created_at=NOW,
                    published_at=NOW,
                    confirmed_at=NOW,
                    profile_version_id=profile_id,
                )
            )
            database.flush()
            database.add(
                ProfileVersionRecord(
                    id=profile_id,
                    owner_user_id=owner_id,
                    import_id=import_id,
                    content_hash=prefix * 64,
                    canonical_content=f"profile-{prefix}".encode(),
                    billing_period_start_utc_ns=0,
                    billing_period_end_utc_ns=1,
                    tariff_timezone="America/Los_Angeles",
                    interval_resolution_seconds=900,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    created_at=NOW,
                )
            )
    return ArtifactHarness(
        sessions=sessions,
        jobs=JobService(sessions),
        artifacts=ArtifactService(sessions, objects),
        objects=objects,
    )


def _add_report_job(
    harness: ArtifactHarness,
    *,
    owner_prefix: str,
    job_id: str,
) -> None:
    owner_id = owner_prefix * 32
    import_id = f"{owner_prefix}i" * 16
    profile_id = f"{owner_prefix}p" * 16
    with harness.sessions.begin() as database:
        database.add(
            JobRecord(
                id=job_id,
                owner_user_id=owner_id,
                kind="REPORT",
                request_schema_version="report-request-v1",
                request_hash="c" * 64,
                request_json="{}",
                scope_mode="ACTIVE_SCOPE",
                import_id=import_id,
                profile_version_id=profile_id,
                captured_account_generation=0,
                captured_import_generation=0,
                captured_profile_generation=0,
                state="QUEUED",
                attempt_count=0,
                max_attempts=3,
                fencing_generation=0,
                not_before=NOW,
                cancel_requested=False,
                created_at=NOW,
            )
        )


def _lease_report(harness: ArtifactHarness, worker_id: str, now: datetime = NOW) -> JobLease:
    lease = harness.jobs.lease_next(
        worker_id=worker_id,
        now=now,
        kinds=frozenset({"REPORT"}),
    )
    assert lease is not None and harness.jobs.start(lease, now=now)
    return lease


def test_upload_is_registered_before_bytes_are_written(
    harness: ArtifactHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_report_job(harness, owner_prefix="1", job_id="a" * 32)
    lease = _lease_report(harness, "worker-a")
    original_put = harness.objects.put_file

    def assert_registered_before_put(key: str, source: BytesIO, *, maximum_bytes: int):  # type: ignore[no-untyped-def]
        with harness.sessions() as database:
            registration = database.scalar(
                select(ObjectUploadRegistrationRecord).where(
                    ObjectUploadRegistrationRecord.object_key == key
                )
            )
            assert registration is not None and registration.state == "REGISTERED"
        return original_put(key, source, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(harness.objects, "put_file", assert_registered_before_put)
    staged = harness.artifacts.stage(
        owner_user_id="1" * 32,
        lease=lease,
        artifact_class="REPORT",
        source=BytesIO(b"redacted report"),
        now=NOW,
    )
    assert staged.size_bytes == len(b"redacted report")
    assert harness.objects.exists(staged.object_key)


def test_fenced_finalize_accepts_once_and_survives_orphan_sweep(
    harness: ArtifactHarness,
) -> None:
    _add_report_job(harness, owner_prefix="1", job_id="a" * 32)
    lease = _lease_report(harness, "worker-a")
    staged = harness.artifacts.stage(
        owner_user_id="1" * 32,
        lease=lease,
        artifact_class="REPORT",
        source=BytesIO(b"redacted report"),
        now=NOW,
    )
    finalized = harness.artifacts.finalize(
        owner_user_id="1" * 32,
        lease=lease,
        semantic_hash="d" * 64,
        calculation_contract_version="report-contract-v1",
        result_type="REPORT",
        result_id="e" * 32,
        artifact_registration_ids=(staged.registration_id,),
        now=NOW,
    )
    repeated = harness.artifacts.finalize(
        owner_user_id="1" * 32,
        lease=lease,
        semantic_hash="d" * 64,
        calculation_contract_version="report-contract-v1",
        result_type="REPORT",
        result_id="e" * 32,
        artifact_registration_ids=(staged.registration_id,),
        now=NOW,
    )
    assert not finalized.repeated
    assert repeated.repeated and repeated.claim_id == finalized.claim_id
    assert (
        harness.artifacts.sweep_orphans(
            now=NOW + timedelta(days=1),
            older_than=NOW + timedelta(days=1),
        )
        == 0
    )
    assert harness.objects.exists(staged.object_key)
    with harness.sessions() as database:
        registration = database.get(ObjectUploadRegistrationRecord, staged.registration_id)
        job = database.get(JobRecord, lease.job_id)
        assert registration is not None and registration.state == "ACCEPTED"
        assert job is not None and job.state == "SUCCEEDED"


def test_replacement_attempt_rejects_stale_finalize_and_sweeps_artifact(
    harness: ArtifactHarness,
) -> None:
    _add_report_job(harness, owner_prefix="1", job_id="a" * 32)
    stale_lease = _lease_report(harness, "worker-old")
    staged = harness.artifacts.stage(
        owner_user_id="1" * 32,
        lease=stale_lease,
        artifact_class="REPORT",
        source=BytesIO(b"stale report"),
        now=NOW,
    )
    replacement_time = NOW + timedelta(seconds=21)
    replacement = _lease_report(harness, "worker-new", replacement_time)
    assert replacement.fencing_generation > stale_lease.fencing_generation
    with pytest.raises(ArtifactServiceError) as raised:
        harness.artifacts.finalize(
            owner_user_id="1" * 32,
            lease=stale_lease,
            semantic_hash="d" * 64,
            calculation_contract_version="report-contract-v1",
            result_type="REPORT",
            result_id="e" * 32,
            artifact_registration_ids=(staged.registration_id,),
            now=replacement_time,
        )
    assert raised.value.code == "STALE_RESULT_ATTEMPT"
    assert (
        harness.artifacts.sweep_orphans(
            now=replacement_time,
            older_than=replacement_time,
        )
        == 1
    )
    assert not harness.objects.exists(staged.object_key)


def test_same_owner_semantic_race_accepts_one_result_and_sweeps_loser(
    harness: ArtifactHarness,
) -> None:
    _add_report_job(harness, owner_prefix="1", job_id="a" * 32)
    _add_report_job(harness, owner_prefix="1", job_id="b" * 32)
    first_lease = _lease_report(harness, "worker-a")
    second_lease = _lease_report(harness, "worker-b")
    first_artifact = harness.artifacts.stage(
        owner_user_id="1" * 32,
        lease=first_lease,
        artifact_class="REPORT",
        source=BytesIO(b"first report"),
        now=NOW,
    )
    second_artifact = harness.artifacts.stage(
        owner_user_id="1" * 32,
        lease=second_lease,
        artifact_class="REPORT",
        source=BytesIO(b"second report"),
        now=NOW,
    )
    accepted = harness.artifacts.finalize(
        owner_user_id="1" * 32,
        lease=first_lease,
        semantic_hash="d" * 64,
        calculation_contract_version="report-contract-v1",
        result_type="REPORT",
        result_id="e" * 32,
        artifact_registration_ids=(first_artifact.registration_id,),
        now=NOW,
    )
    reused = harness.artifacts.finalize(
        owner_user_id="1" * 32,
        lease=second_lease,
        semantic_hash="d" * 64,
        calculation_contract_version="report-contract-v1",
        result_type="REPORT",
        result_id="f" * 32,
        artifact_registration_ids=(second_artifact.registration_id,),
        now=NOW,
    )
    assert reused.repeated and reused.claim_id == accepted.claim_id
    assert reused.result_id == "e" * 32
    assert (
        harness.artifacts.sweep_orphans(
            now=NOW + timedelta(seconds=1),
            older_than=NOW + timedelta(seconds=1),
        )
        == 1
    )
    assert harness.objects.exists(first_artifact.object_key)
    assert not harness.objects.exists(second_artifact.object_key)
    with harness.sessions() as database:
        claim_count = database.scalar(select(func.count()).select_from(JobResultClaimRecord))
        assert claim_count == 1


def test_identical_semantics_are_owned_separately_across_accounts(
    harness: ArtifactHarness,
) -> None:
    _add_report_job(harness, owner_prefix="1", job_id="a" * 32)
    _add_report_job(harness, owner_prefix="2", job_id="b" * 32)
    first_lease = _lease_report(harness, "worker-a")
    second_lease = _lease_report(harness, "worker-b")
    first = harness.artifacts.finalize(
        owner_user_id="1" * 32,
        lease=first_lease,
        semantic_hash="d" * 64,
        calculation_contract_version="report-contract-v1",
        result_type="REPORT",
        result_id="e" * 32,
        artifact_registration_ids=(),
        now=NOW,
    )
    second = harness.artifacts.finalize(
        owner_user_id="2" * 32,
        lease=second_lease,
        semantic_hash="d" * 64,
        calculation_contract_version="report-contract-v1",
        result_type="REPORT",
        result_id="f" * 32,
        artifact_registration_ids=(),
        now=NOW,
    )
    assert not first.repeated and not second.repeated
    assert first.claim_id != second.claim_id


def test_storage_failure_leaves_registered_artifact_for_cleanup(
    harness: ArtifactHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_report_job(harness, owner_prefix="1", job_id="a" * 32)
    lease = _lease_report(harness, "worker-a")

    def fail_put(_key: str, _source: BytesIO, *, maximum_bytes: int):  # type: ignore[no-untyped-def]
        assert maximum_bytes > 0
        raise ObjectStoreError("INJECTED_STORAGE_FAILURE", "injected")

    monkeypatch.setattr(harness.objects, "put_file", fail_put)
    with pytest.raises(ObjectStoreError, match="injected"):
        harness.artifacts.stage(
            owner_user_id="1" * 32,
            lease=lease,
            artifact_class="REPORT",
            source=BytesIO(b"report"),
            now=NOW,
        )
    with harness.sessions() as database:
        registration = database.scalar(select(ObjectUploadRegistrationRecord))
        assert registration is not None and registration.state == "REGISTERED"
    assert (
        harness.artifacts.sweep_orphans(
            now=NOW + timedelta(seconds=21),
            older_than=NOW + timedelta(seconds=21),
        )
        == 1
    )


def test_domain_result_callback_rolls_back_with_terminal_publication(
    harness: ArtifactHarness,
) -> None:
    _add_report_job(harness, owner_prefix="1", job_id="a" * 32)
    lease = _lease_report(harness, "worker-a")
    staged = harness.artifacts.stage(
        owner_user_id="1" * 32,
        lease=lease,
        artifact_class="REPORT",
        source=BytesIO(b"redacted report"),
        now=NOW,
    )

    def fail_publication(_database: Session) -> None:
        raise RuntimeError("injected domain publication failure")

    with pytest.raises(RuntimeError, match="injected domain publication failure"):
        harness.artifacts.finalize(
            owner_user_id="1" * 32,
            lease=lease,
            semantic_hash="d" * 64,
            calculation_contract_version="report-contract-v1",
            result_type="REPORT",
            result_id="e" * 32,
            artifact_registration_ids=(staged.registration_id,),
            now=NOW,
            publish_result=fail_publication,
        )
    with harness.sessions() as database:
        job = database.get(JobRecord, lease.job_id)
        registration = database.get(ObjectUploadRegistrationRecord, staged.registration_id)
        claim_count = database.scalar(select(func.count()).select_from(JobResultClaimRecord))
        assert job is not None and job.state == "RUNNING"
        assert registration is not None and registration.state == "STAGED"
        assert claim_count == 0
