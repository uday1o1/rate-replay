from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from ratereplay_persistence.comparisons import ComparisonService
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ComparisonResultRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
    UserRecord,
)
from ratereplay_persistence.replays import ReplayService
from ratereplay_tariffs.admission import load_admitted_e1, load_all_admitted_tariffs
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    ReplayRequest,
    replay_compiled_tariff,
)
from ratereplay_tariffs.comparison import compare_admitted_tariffs, load_required_component_keys
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts
from sqlalchemy import delete
from sqlalchemy.exc import StatementError

ROOT = Path(__file__).resolve().parents[2]
PROFILE_HASH = "47b449f47039960cde24666a5ed2723781b7773d624dbdd2b74de78e02da19ce"


def _comparison_request() -> IntervalReplayRequest:
    facts = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
        ),
    )
    profile = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "data/demo/july-2026-simulated-profile.json").read_text(encoding="utf-8")
        ),
    )
    readings = cast(list[dict[str, Any]], profile["readings"])
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256=PROFILE_HASH,
        account_facts=AccountFacts.model_validate_json(json.dumps(facts["account_facts"])),
        energy_wh=cast(int, profile["total_energy_wh"]),
        intervals=tuple(
            ReplayInterval(
                start_utc_ns=int(
                    datetime.fromisoformat(
                        cast(str, reading["start_utc"]).replace("Z", "+00:00")
                    ).timestamp()
                )
                * 1_000_000_000,
                duration_seconds=cast(int, reading["duration_seconds"]),
                energy_wh=cast(int, reading["energy_wh"]),
            )
            for reading in readings
        ),
        dated_eligibility_facts=DatedEligibilityFacts.model_validate_json(
            json.dumps(facts["dated_eligibility_facts"])
        ),
    )


@pytest.mark.postgres
def test_migrated_postgres_publishes_immutable_comparison() -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    engine = make_engine(database_url)
    sessions = make_session_factory(engine)
    owner_id = secrets.token_hex(16)
    import_id = secrets.token_hex(16)
    profile_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    start_ns = int(datetime(2026, 7, 1, 7, tzinfo=UTC).timestamp()) * 1_000_000_000
    end_ns = int(datetime(2026, 8, 1, 7, tzinfo=UTC).timestamp()) * 1_000_000_000
    request = _comparison_request()
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=owner_id,
                username_canonical=f"comparison_{secrets.token_hex(5)}",
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
                adapter="SIMULATED_PROFILE_V1",
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
                content_hash=PROFILE_HASH,
                canonical_content=b"postgres-comparison-integration",
                billing_period_start_utc_ns=start_ns,
                billing_period_end_utc_ns=end_ns,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=900,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )

    replay_request = ReplayRequest(
        request_version="e1-replay-request-v1",
        profile_content_sha256=PROFILE_HASH,
        account_facts=request.account_facts,
        energy_wh=request.energy_wh,
    )
    replay_result = replay_compiled_tariff(
        load_admitted_e1(ROOT).compilation,
        replay_request,
    )
    replay_operation_hash = canonical_content_sha256(
        b"RateReplay.ReplayOperationRequest.v1", replay_request.model_dump(mode="json")
    )
    replay_service = ReplayService(sessions)
    stored_replay = replay_service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="postgres-comparison-replay",
        operation_request_hash=replay_operation_hash,
        result=replay_result,
        now=now,
    )
    result = compare_admitted_tariffs(
        load_all_admitted_tariffs(ROOT),
        request,
        current_tariff_version_id="pge-e1-2026-07",
        required_component_keys=load_required_component_keys(ROOT),
    )
    comparison_operation_hash = canonical_content_sha256(
        b"RateReplay.ComparisonOperationRequest.v1", request.model_dump(mode="json")
    )
    comparison_service = ComparisonService(sessions)
    stored = comparison_service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        current_replay_id=stored_replay.replay_id,
        idempotency_key="postgres-comparison-key",
        operation_request_hash=comparison_operation_hash,
        result=result,
        now=now,
    )
    repeated = comparison_service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        current_replay_id=stored_replay.replay_id,
        idempotency_key="postgres-comparison-key",
        operation_request_hash=comparison_operation_hash,
        result=result,
        now=now,
    )
    assert repeated.repeated is True
    assert repeated.comparison_id == stored.comparison_id
    with sessions() as database:
        comparison = database.get(ComparisonResultRecord, stored.comparison_id)
        job = database.get(JobRecord, stored.job_id)
        assert comparison is not None and comparison.result_hash == result.comparison_sha256
        assert comparison.current_replay_id == stored_replay.replay_id
        assert job is not None and job.kind == "COMPARISON" and job.state == "SUCCEEDED"
        comparison.result_json = "{}"
        with pytest.raises((RuntimeError, StatementError), match="immutable"):
            database.commit()
        database.rollback()

    with sessions.begin() as database:
        database.execute(
            delete(ComparisonResultRecord).where(ComparisonResultRecord.id == stored.comparison_id)
        )
        database.execute(
            delete(CalculationManifestRecord).where(
                CalculationManifestRecord.replay_id == stored_replay.replay_id
            )
        )
        database.execute(
            delete(ReplayResultRecord).where(ReplayResultRecord.id == stored_replay.replay_id)
        )
        database.execute(
            delete(OperationRequestRecord).where(OperationRequestRecord.owner_user_id == owner_id)
        )
        database.execute(
            delete(JobAttemptRecord).where(
                JobAttemptRecord.job_id.in_([stored.job_id, stored_replay.job_id])
            )
        )
        database.execute(
            delete(JobRecord).where(JobRecord.id.in_([stored.job_id, stored_replay.job_id]))
        )
        database.execute(delete(ProfileVersionRecord).where(ProfileVersionRecord.id == profile_id))
        database.execute(delete(ImportRecord).where(ImportRecord.id == import_id))
        database.execute(delete(UserRecord).where(UserRecord.id == owner_id))
    engine.dispose()
