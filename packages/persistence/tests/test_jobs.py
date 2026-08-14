from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.jobs import JOB_DEFINITIONS, JobService
from ratereplay_persistence.models import (
    ImportRecord,
    JobRecord,
    ProfileVersionRecord,
    UserRecord,
)
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, tzinfo=UTC)
OWNER_ID = "1" * 32
IMPORT_ID = "2" * 32
PROFILE_ID = "3" * 32


@dataclass(frozen=True, slots=True)
class JobHarness:
    sessions: sessionmaker[Session]
    jobs: JobService


@pytest.fixture
def harness() -> JobHarness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=OWNER_ID,
                username_canonical="job_owner",
                password_hash="test-only",
                created_at=NOW,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
            )
        )
        database.flush()
        database.add(
            ImportRecord(
                id=IMPORT_ID,
                owner_user_id=OWNER_ID,
                state="CONFIRMED",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                adapter="TEST_CANONICAL",
                raw_content_hash="a" * 64,
                created_at=NOW,
                published_at=NOW,
                confirmed_at=NOW,
                profile_version_id=PROFILE_ID,
            )
        )
        database.flush()
        database.add(
            ProfileVersionRecord(
                id=PROFILE_ID,
                owner_user_id=OWNER_ID,
                import_id=IMPORT_ID,
                content_hash="b" * 64,
                canonical_content=b"profile",
                billing_period_start_utc_ns=0,
                billing_period_end_utc_ns=1,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=900,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=NOW,
            )
        )
    return JobHarness(sessions=sessions, jobs=JobService(sessions))


def _add_job(
    harness: JobHarness,
    *,
    job_id: str,
    kind: str,
    scope_mode: str,
    owner_user_id: str | None,
    import_id: str | None,
    profile_version_id: str | None,
    account_generation: int,
    import_generation: int | None,
    profile_generation: int | None,
    max_attempts: int = 3,
) -> None:
    with harness.sessions.begin() as database:
        database.add(
            JobRecord(
                id=job_id,
                owner_user_id=owner_user_id,
                kind=kind,
                request_schema_version=f"{kind.lower()}-request-v1",
                request_hash="c" * 64,
                request_json="{}",
                scope_mode=scope_mode,
                import_id=import_id,
                profile_version_id=profile_version_id,
                captured_account_generation=account_generation,
                captured_import_generation=import_generation,
                captured_profile_generation=profile_generation,
                state="QUEUED",
                attempt_count=0,
                max_attempts=max_attempts,
                fencing_generation=0,
                not_before=NOW,
                cancel_requested=False,
                created_at=NOW,
            )
        )


def test_job_registry_fixes_every_v1_kind_to_one_scope_mode() -> None:
    assert {kind: definition.scope_mode for kind, definition in JOB_DEFINITIONS.items()} == {
        "IMPORT": "ACTIVE_SCOPE",
        "REPLAY": "ACTIVE_SCOPE",
        "COMPARISON": "ACTIVE_SCOPE",
        "SCENARIO": "ACTIVE_SCOPE",
        "REPORT": "ACTIVE_SCOPE",
        "RETENTION": "SYSTEM_SCOPE",
        "DELETION": "DELETING_SCOPE",
    }


def test_active_compute_lease_is_fenced_by_profile_generation(harness: JobHarness) -> None:
    _add_job(
        harness,
        job_id="replay-job",
        kind="REPLAY",
        scope_mode="ACTIVE_SCOPE",
        owner_user_id=OWNER_ID,
        import_id=IMPORT_ID,
        profile_version_id=PROFILE_ID,
        account_generation=0,
        import_generation=0,
        profile_generation=0,
    )
    lease = harness.jobs.lease_next(
        worker_id="replay-worker",
        now=NOW,
        kinds=frozenset({"REPLAY"}),
    )
    assert lease is not None and lease.kind == "REPLAY"
    assert lease.profile_version_id == PROFILE_ID
    assert harness.jobs.start(lease, now=NOW)
    with pytest.raises(ValueError, match="SYSTEM_SCOPE retention"):
        harness.jobs.complete_system(lease, now=NOW)
    with harness.sessions.begin() as database:
        profile = database.get(ProfileVersionRecord, PROFILE_ID)
        assert profile is not None
        profile.lifecycle_generation += 1
    assert not harness.jobs.heartbeat(lease, now=NOW + timedelta(seconds=1))
    assert harness.jobs.rescue_expired(now=NOW + timedelta(seconds=21)) == 1
    with harness.sessions() as database:
        job = database.get(JobRecord, "replay-job")
        assert job is not None
        assert (job.state, job.failure_code) == ("CANCELLED", "SCOPE_FENCED")


def test_system_retention_job_has_no_user_data_scope(harness: JobHarness) -> None:
    _add_job(
        harness,
        job_id="retention-job",
        kind="RETENTION",
        scope_mode="SYSTEM_SCOPE",
        owner_user_id=None,
        import_id=None,
        profile_version_id=None,
        account_generation=0,
        import_generation=None,
        profile_generation=None,
    )
    lease = harness.jobs.lease_next(
        worker_id="retention-worker",
        now=NOW,
        kinds=frozenset({"RETENTION"}),
    )
    assert lease is not None
    assert lease.scope_mode == "SYSTEM_SCOPE"
    assert lease.import_id is None and lease.profile_version_id is None
    assert harness.jobs.start(lease, now=NOW)
    assert harness.jobs.complete_system(lease, now=NOW)
    assert not harness.jobs.complete_system(lease, now=NOW)
    with harness.sessions() as database:
        job = database.get(JobRecord, "retention-job")
        assert job is not None and job.state == "SUCCEEDED"


def test_deletion_job_runs_only_at_exact_deleting_generation(harness: JobHarness) -> None:
    with harness.sessions.begin() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None
        user.lifecycle_state = "DELETING"
        user.lifecycle_generation = 4
    _add_job(
        harness,
        job_id="deletion-job",
        kind="DELETION",
        scope_mode="DELETING_SCOPE",
        owner_user_id=OWNER_ID,
        import_id=None,
        profile_version_id=None,
        account_generation=4,
        import_generation=None,
        profile_generation=None,
    )
    lease = harness.jobs.lease_next(
        worker_id="deletion-worker",
        now=NOW,
        kinds=frozenset({"DELETION"}),
    )
    assert lease is not None and harness.jobs.start(lease, now=NOW)
    with harness.sessions.begin() as database:
        user = database.get(UserRecord, OWNER_ID)
        assert user is not None
        user.lifecycle_generation = 5
    assert not harness.jobs.heartbeat(lease, now=NOW + timedelta(seconds=1))


def test_mismatched_scope_mode_is_rejected_before_valid_job(harness: JobHarness) -> None:
    _add_job(
        harness,
        job_id="bad-retention",
        kind="RETENTION",
        scope_mode="ACTIVE_SCOPE",
        owner_user_id=OWNER_ID,
        import_id=None,
        profile_version_id=None,
        account_generation=0,
        import_generation=None,
        profile_generation=None,
    )
    _add_job(
        harness,
        job_id="good-retention",
        kind="RETENTION",
        scope_mode="SYSTEM_SCOPE",
        owner_user_id=None,
        import_id=None,
        profile_version_id=None,
        account_generation=0,
        import_generation=None,
        profile_generation=None,
    )
    lease = harness.jobs.lease_next(
        worker_id="retention-worker",
        now=NOW,
        kinds=frozenset({"RETENTION"}),
    )
    assert lease is not None and lease.job_id == "good-retention"
    with harness.sessions() as database:
        rejected = database.get(JobRecord, "bad-retention")
        assert rejected is not None
        assert (rejected.state, rejected.failure_code) == ("CANCELLED", "SCOPE_FENCED")


def test_unknown_worker_kind_is_rejected(harness: JobHarness) -> None:
    with pytest.raises(ValueError, match="Unknown job kinds: UNKNOWN"):
        harness.jobs.lease_next(
            worker_id="worker",
            now=NOW,
            kinds=frozenset({"UNKNOWN"}),
        )
