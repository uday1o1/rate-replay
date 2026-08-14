from __future__ import annotations

import os
import secrets
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    RawObjectRecord,
    ReplayResultRecord,
    UserRecord,
)
from ratereplay_persistence.replays import ReplayService
from ratereplay_tariffs.admission import load_admitted_e1
from ratereplay_tariffs.billing import ReplayRequest, replay_compiled_tariff
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts, DateRange
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.postgres
def test_migrated_postgres_publishes_immutable_replay() -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    owner_id = secrets.token_hex(16)
    import_id = secrets.token_hex(16)
    profile_id = secrets.token_hex(16)
    profile_hash = secrets.token_hex(32)
    now = datetime.now(UTC)
    start_ns = int(datetime(2026, 7, 1, 7, tzinfo=UTC).timestamp()) * 1_000_000_000
    end_ns = int(datetime(2026, 8, 1, 7, tzinfo=UTC).timestamp()) * 1_000_000_000
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=owner_id,
                username_canonical=f"replay_{secrets.token_hex(5)}",
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
                raw_content_hash=secrets.token_hex(32),
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
                content_hash=profile_hash,
                canonical_content=b"postgres-replay-integration",
                billing_period_start_utc_ns=start_ns,
                billing_period_end_utc_ns=end_ns,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=86_400,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )

    account_facts = AccountFacts(
        schema_version="account-facts-v1",
        service_window=DateRange(start=date(2026, 7, 1), end=date(2026, 8, 1)),
        service_provider="PG&E",
        service_mode="BUNDLED",
        meter_count=1,
        primary_meter_only=True,
        income_tier="TIER_3",
        care_enrolled=False,
        fera_enrolled=False,
        medical_baseline=False,
        cca_service=False,
        direct_access_service=False,
        active_bill_protection=False,
        solar_or_export=False,
        baseline_territory="T",
        baseline_quantity_code="BASIC",
        qualifying_technologies=(),
        user_attested_at=now,
    )
    request = ReplayRequest(
        request_version="e1-replay-request-v1",
        profile_content_sha256=profile_hash,
        account_facts=account_facts,
        energy_wh=310_000,
    )
    result = replay_compiled_tariff(load_admitted_e1(ROOT).compilation, request)
    operation_hash = canonical_content_sha256(
        b"RateReplay.ReplayOperationRequest.v1", request.model_dump(mode="json")
    )
    service = ReplayService(sessions)
    stored = service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="postgres-replay-key",
        operation_request_hash=operation_hash,
        result=result,
        now=now,
    )
    repeated = service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="postgres-replay-key",
        operation_request_hash=operation_hash,
        result=result,
        now=now,
    )
    assert repeated.repeated is True
    assert repeated.replay_id == stored.replay_id
    with sessions() as database:
        replay = database.get(ReplayResultRecord, stored.replay_id)
        manifest = database.scalar(
            select(CalculationManifestRecord).where(
                CalculationManifestRecord.replay_id == stored.replay_id
            )
        )
        job = database.get(JobRecord, stored.job_id)
        assert replay is not None and replay.result_hash == result.result_sha256
        assert manifest is not None and manifest.calculation_hash == (
            result.manifest.calculation_sha256
        )
        assert job is not None and job.state == "SUCCEEDED"

    with sessions.begin() as database:
        database.execute(
            delete(CalculationManifestRecord).where(
                CalculationManifestRecord.replay_id == stored.replay_id
            )
        )
        database.execute(
            delete(ReplayResultRecord).where(ReplayResultRecord.id == stored.replay_id)
        )
        database.execute(
            delete(OperationRequestRecord).where(OperationRequestRecord.owner_user_id == owner_id)
        )
        database.execute(delete(JobAttemptRecord).where(JobAttemptRecord.job_id == stored.job_id))
        database.execute(delete(JobRecord).where(JobRecord.id == stored.job_id))
        database.execute(
            delete(ImportReadingRecord).where(ImportReadingRecord.import_id == import_id)
        )
        database.execute(delete(ProfileVersionRecord).where(ProfileVersionRecord.id == profile_id))
        database.execute(delete(RawObjectRecord).where(RawObjectRecord.import_id == import_id))
        database.execute(delete(ImportRecord).where(ImportRecord.id == import_id))
        database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
    engine.dispose()
