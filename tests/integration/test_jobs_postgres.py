from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    ProfileVersionRecord,
    UserRecord,
)
from sqlalchemy import delete

pytestmark = pytest.mark.postgres


def test_postgres_skip_locked_leases_distinct_profile_fenced_jobs() -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    now = datetime.now(UTC)
    owner_id = secrets.token_hex(16)
    import_id = secrets.token_hex(16)
    profile_id = secrets.token_hex(16)
    job_ids = (secrets.token_hex(16), secrets.token_hex(16))
    try:
        with sessions.begin() as database:
            database.add(
                UserRecord(
                    id=owner_id,
                    username_canonical=f"jobs_{owner_id}",
                    password_hash="test-only",
                    created_at=now,
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
                    created_at=now,
                    published_at=now,
                    confirmed_at=now,
                    profile_version_id=profile_id,
                )
            )
            database.flush()
            database.add(
                ProfileVersionRecord(
                    id=profile_id,
                    owner_user_id=owner_id,
                    import_id=import_id,
                    content_hash="b" * 64,
                    canonical_content=b"postgres-job-profile",
                    billing_period_start_utc_ns=0,
                    billing_period_end_utc_ns=1,
                    tariff_timezone="America/Los_Angeles",
                    interval_resolution_seconds=900,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    created_at=now,
                )
            )
            database.flush()
            database.add_all(
                [
                    JobRecord(
                        id=job_id,
                        owner_user_id=owner_id,
                        kind="REPLAY",
                        request_schema_version="replay-request-v1",
                        request_hash=character * 64,
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
                        not_before=now,
                        cancel_requested=False,
                        created_at=now,
                    )
                    for job_id, character in zip(job_ids, ("c", "d"), strict=True)
                ]
            )

        def lease(worker_id: str) -> str | None:
            acquired = JobService(sessions).lease_next(
                worker_id=worker_id,
                now=now,
                kinds=frozenset({"REPLAY"}),
            )
            return acquired.job_id if acquired is not None else None

        with ThreadPoolExecutor(max_workers=2) as executor:
            leased_ids = tuple(executor.map(lease, ("worker-a", "worker-b")))
        assert set(leased_ids) == set(job_ids)
        with sessions() as database:
            rows = [database.get(JobRecord, job_id) for job_id in job_ids]
            assert all(row is not None and row.state == "LEASED" for row in rows)
            assert {row.lease_owner for row in rows if row is not None} == {
                "worker-a",
                "worker-b",
            }
    finally:
        with sessions.begin() as database:
            database.execute(delete(JobAttemptRecord).where(JobAttemptRecord.job_id.in_(job_ids)))
            database.execute(delete(JobRecord).where(JobRecord.id.in_(job_ids)))
            database.execute(
                delete(ProfileVersionRecord).where(ProfileVersionRecord.id == profile_id)
            )
            database.execute(delete(ImportRecord).where(ImportRecord.id == import_id))
            database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
        engine.dispose()
