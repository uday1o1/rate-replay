from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ReferenceSlot,
    ScenarioInput,
    ValidatedScenario,
)
from ratereplay_optimizer.results import ScenarioOptimizationResult, build_scenario_result
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import optimize_exact, optimize_off_peak_heuristic
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    ProfileVersionRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
    ScenarioResultRecord,
    UserRecord,
)
from ratereplay_persistence.scenarios import ScenarioService, ScenarioServiceError
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError

ROOT = Path(__file__).resolve().parents[3]


def _result() -> tuple[ValidatedScenario, ScenarioOptimizationResult]:
    bundle = compile_tariff(ROOT, ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    facts_payload = json.loads(
        (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
    )
    account = AccountFacts.model_validate_json(json.dumps(facts_payload["account_facts"]))
    dated = DatedEligibilityFacts.model_validate_json(
        json.dumps(facts_payload["dated_eligibility_facts"])
    )
    start = datetime(2026, 7, 6, 22, tzinfo=UTC)
    reference = (0, 0, 70)
    slots = tuple(
        CanonicalProfileSlot(
            slot_start_utc=start + timedelta(hours=index),
            duration_seconds=3_600,
            measured_energy_wh=100 + amount,
        )
        for index, amount in enumerate(reference)
    )
    load = FlexibleLoad(
        load_id=UUID("00000000-0000-0000-0000-000000000001"),
        physical_asset_key="ev-1",
        kind="EV",
        mode="SHIFT_EXISTING",
        execution_spec=InterruptibleModulatingSpec(
            execution_type="INTERRUPTIBLE_MODULATING",
            maximum_power_w=70,
            minimum_power_when_active_w=0,
        ),
        occurrences=(
            LoadOccurrence(
                occurrence_id=UUID("10000000-0000-0000-0000-000000000001"),
                required_energy_wh=70,
                earliest_start_utc=slots[0].slot_start_utc,
                deadline_utc=slots[-1].slot_start_utc + timedelta(hours=1),
                reference_schedule=tuple(
                    ReferenceSlot(
                        slot_start_utc=slot.slot_start_utc,
                        duration_seconds=slot.duration_seconds,
                        energy_wh=reference[index],
                    )
                    for index, slot in enumerate(slots)
                ),
            ),
        ),
    )
    validated = validate_and_decompose_scenario(
        ScenarioInput(
            scenario_version="historical-flex-scenario-v1",
            profile_content_sha256="a" * 64,
            tariff_version_id=bundle.ir.tariff_version_id,
            profile_slots=slots,
            loads=(load,),
        )
    )
    exact = optimize_exact(validated, bundle, account, dated_facts=dated)
    heuristic = optimize_off_peak_heuristic(validated, bundle, account, dated_facts=dated)
    return validated, build_scenario_result(validated, bundle, account, dated, exact, heuristic)


def test_scenario_publication_is_normalized_immutable_idempotent_and_owner_scoped() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    owner_id = secrets.token_hex(16)
    import_id = secrets.token_hex(16)
    profile_id = secrets.token_hex(16)
    now = datetime.now(UTC)
    with sessions.begin() as database:
        database.add(
            UserRecord(
                id=owner_id,
                username_canonical="scenario_owner",
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
                raw_content_hash="b" * 64,
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
                content_hash="a" * 64,
                canonical_content=b"scenario-profile",
                billing_period_start_utc_ns=0,
                billing_period_end_utc_ns=1,
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=3_600,
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
        )
    validated, result = _result()
    service = ScenarioService(sessions)
    stored = service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="scenario-request-one",
        operation_request_hash="c" * 64,
        validated=validated,
        result=result,
        now=now,
    )
    repeated = service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="scenario-request-one",
        operation_request_hash="c" * 64,
        validated=validated,
        result=result,
        now=now,
    )
    semantic_reuse = service.publish(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="scenario-request-two",
        operation_request_hash="c" * 64,
        validated=validated,
        result=result,
        now=now,
    )

    assert repeated == semantic_reuse
    assert repeated.repeated is True
    assert repeated.scenario_id == stored.scenario_id
    with sessions() as database:
        scenario = database.get(ScenarioRecord, stored.scenario_id)
        scenario_result = database.get(ScenarioResultRecord, stored.result_id)
        job = database.get(JobRecord, stored.job_id)
        attempt = database.scalar(
            select(JobAttemptRecord).where(JobAttemptRecord.job_id == stored.job_id)
        )
        manifest = database.scalar(
            select(CalculationManifestRecord).where(
                CalculationManifestRecord.scenario_result_id == stored.result_id
            )
        )
        assert scenario is not None and scenario.state == "SUCCEEDED"
        assert scenario_result is not None and scenario_result.result_hash == result.result_sha256
        assert job is not None and job.kind == "SCENARIO" and job.state == "SUCCEEDED"
        assert attempt is not None and attempt.worker_id == "inline-verified-optimizer"
        assert manifest is not None and manifest.replay_id is None
        assert database.scalar(select(func.count()).select_from(ScenarioLoadRecord)) == 1
        assert (
            database.scalar(select(func.count()).select_from(ScenarioReferenceScheduleRecord)) == 1
        )
        scenario_result.result_json = "{}"
        with pytest.raises((RuntimeError, StatementError), match="immutable"):
            database.commit()
        database.rollback()

    with pytest.raises(ScenarioServiceError) as terminal:
        service.cancel(owner_user_id=owner_id, scenario_id=stored.scenario_id)
    assert terminal.value.code == "SCENARIO_ALREADY_TERMINAL"
    with pytest.raises(ScenarioServiceError) as reused_key:
        service.publish(
            owner_user_id=owner_id,
            profile_version_id=profile_id,
            idempotency_key="scenario-request-one",
            operation_request_hash="d" * 64,
            validated=validated,
            result=result,
            now=now,
        )
    assert reused_key.value.code == "IDEMPOTENCY_KEY_REUSED"
    with pytest.raises(ScenarioServiceError) as hidden:
        service.cancel(owner_user_id=secrets.token_hex(16), scenario_id=stored.scenario_id)
    assert hidden.value.code == "SCENARIO_NOT_FOUND"
    engine.dispose()
