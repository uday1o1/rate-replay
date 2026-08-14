from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    JobResultClaimRecord,
    ObjectUploadRegistrationRecord,
    ProfileVersionRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore
from sqlalchemy import delete, func, select

pytestmark = pytest.mark.postgres


def test_postgres_skip_locked_and_semantic_publication_race(tmp_path: Path) -> None:
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

        def lease(worker_id: str) -> JobLease | None:
            return JobService(sessions).lease_next(
                worker_id=worker_id,
                now=now,
                kinds=frozenset({"REPLAY"}),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            leases = tuple(executor.map(lease, ("worker-a", "worker-b")))
        assert all(acquired is not None for acquired in leases)
        live_leases = tuple(acquired for acquired in leases if acquired is not None)
        assert {acquired.job_id for acquired in live_leases} == set(job_ids)
        jobs = JobService(sessions)
        assert all(jobs.start(acquired, now=now) for acquired in live_leases)
        artifacts = ArtifactService(sessions, FilesystemObjectStore(tmp_path / "objects"))
        staged = tuple(
            artifacts.stage(
                owner_user_id=owner_id,
                lease=acquired,
                artifact_class="TRACE",
                source=BytesIO(f"trace-{index}".encode()),
                now=now,
            )
            for index, acquired in enumerate(live_leases)
        )

        def finalize(arguments):  # type: ignore[no-untyped-def]
            acquired, artifact, result_id = arguments
            return artifacts.finalize(
                owner_user_id=owner_id,
                lease=acquired,
                semantic_hash="e" * 64,
                calculation_contract_version="replay-contract-v1",
                result_type="REPLAY",
                result_id=result_id,
                artifact_registration_ids=(artifact.registration_id,),
                now=now,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(
                    finalize,
                    zip(live_leases, staged, ("f" * 32, "0" * 32), strict=True),
                )
            )
        assert len({result.claim_id for result in results}) == 1
        assert sorted(result.repeated for result in results) == [False, True]
        with sessions() as database:
            rows = [database.get(JobRecord, job_id) for job_id in job_ids]
            assert all(row is not None and row.state == "SUCCEEDED" for row in rows)
            assert {row.lease_owner for row in rows if row is not None} == {
                "worker-a",
                "worker-b",
            }
            claim_count = database.scalar(
                select(func.count())
                .select_from(JobResultClaimRecord)
                .where(JobResultClaimRecord.accepted_job_id.in_(job_ids))
            )
            accepted_count = database.scalar(
                select(func.count())
                .select_from(ObjectUploadRegistrationRecord)
                .where(
                    ObjectUploadRegistrationRecord.job_id.in_(job_ids),
                    ObjectUploadRegistrationRecord.state == "ACCEPTED",
                )
            )
            assert claim_count == 1 and accepted_count == 1
    finally:
        with sessions.begin() as database:
            database.execute(
                delete(ObjectUploadRegistrationRecord).where(
                    ObjectUploadRegistrationRecord.job_id.in_(job_ids)
                )
            )
            database.execute(
                delete(JobResultClaimRecord).where(
                    JobResultClaimRecord.accepted_job_id.in_(job_ids)
                )
            )
            database.execute(delete(JobAttemptRecord).where(JobAttemptRecord.job_id.in_(job_ids)))
            database.execute(delete(JobRecord).where(JobRecord.id.in_(job_ids)))
            database.execute(
                delete(ProfileVersionRecord).where(ProfileVersionRecord.id == profile_id)
            )
            database.execute(delete(ImportRecord).where(ImportRecord.id == import_id))
            database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
        engine.dispose()
