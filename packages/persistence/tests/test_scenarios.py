from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
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
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import default_solver_configuration
from ratereplay_persistence.calculations import CalculationSubmission
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.models import (
    ImportRecord,
    JobRecord,
    ProfileVersionRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
    UserRecord,
)
from ratereplay_persistence.scenarios import ScenarioService, ScenarioServiceError
from ratereplay_tariffs.admission import AdmittedTariff, load_all_admitted_tariffs
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError

ROOT = Path(__file__).resolve().parents[3]


def _inputs() -> tuple[
    ValidatedScenario,
    AdmittedTariff,
    AccountFacts,
    DatedEligibilityFacts,
]:
    tariff = next(
        item
        for item in load_all_admitted_tariffs(ROOT)
        if item.lock.tariff_version_id == "pge-etoud-2026-07"
    )
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
            tariff_version_id=tariff.lock.tariff_version_id,
            profile_slots=slots,
            loads=(load,),
        )
    )
    return validated, tariff, account, dated


def test_scenario_submission_is_normalized_immutable_idempotent_and_owner_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    validated, tariff, account, dated = _inputs()
    configuration = default_solver_configuration(max_deterministic_time_per_stage=5.0)
    service = ScenarioService(sessions)
    original_submit = service._submissions.submit
    companion_visible_when_job_commits: list[bool] = []

    def observe_submission(**kwargs: Any) -> CalculationSubmission:
        submission = original_submit(**kwargs)
        with sessions() as database:
            companion_visible_when_job_commits.append(
                database.scalar(
                    select(func.count(ScenarioRecord.id)).where(
                        ScenarioRecord.job_id == submission.job_id
                    )
                )
                == 1
            )
        return submission

    monkeypatch.setattr(service._submissions, "submit", observe_submission)
    stored = service.submit(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="scenario-request-one",
        tariff=tariff,
        account_facts=account,
        dated_facts=dated,
        validated=validated,
        attestation_load_ids=("00000000-0000-0000-0000-000000000001",),
        solver_configuration=configuration,
        environment_lock_hash="e" * 64,
        now=now,
    )
    repeated = service.submit(
        owner_user_id=owner_id,
        profile_version_id=profile_id,
        idempotency_key="scenario-request-one",
        tariff=tariff,
        account_facts=account,
        dated_facts=dated,
        validated=validated,
        attestation_load_ids=("00000000-0000-0000-0000-000000000001",),
        solver_configuration=configuration,
        environment_lock_hash="e" * 64,
        now=now,
    )

    assert repeated.calculation.repeated_operation is True
    assert repeated.calculation.semantic_reuse is False
    assert repeated.scenario_id == stored.scenario_id
    assert companion_visible_when_job_commits == [True, True]
    with sessions() as database:
        scenario = database.get(ScenarioRecord, stored.scenario_id)
        job = database.get(JobRecord, stored.job_id)
        assert scenario is not None and scenario.state == "QUEUED"
        assert scenario.input_json == validated.scenario.model_dump_json()
        assert job is not None and job.kind == "SCENARIO" and job.state == "QUEUED"
        assert database.scalar(select(func.count()).select_from(ScenarioLoadRecord)) == 1
        assert (
            database.scalar(select(func.count()).select_from(ScenarioReferenceScheduleRecord)) == 1
        )
        scenario.input_json = "{}"
        with pytest.raises((RuntimeError, StatementError), match="immutable"):
            database.commit()
        database.rollback()

    service.cancel(owner_user_id=owner_id, scenario_id=stored.scenario_id, now=now)
    with sessions() as database:
        scenario = database.get(ScenarioRecord, stored.scenario_id)
        job = database.get(JobRecord, stored.job_id)
        assert scenario is not None and scenario.state == "CANCELLED"
        assert job is not None and job.state == "CANCELLED"
    with pytest.raises(ScenarioServiceError) as terminal:
        service.cancel(owner_user_id=owner_id, scenario_id=stored.scenario_id, now=now)
    assert terminal.value.code == "SCENARIO_ALREADY_TERMINAL"
    with pytest.raises(ScenarioServiceError) as reused_key:
        service.submit(
            owner_user_id=owner_id,
            profile_version_id=profile_id,
            idempotency_key="scenario-request-one",
            tariff=tariff,
            account_facts=account,
            dated_facts=dated,
            validated=validated,
            attestation_load_ids=("00000000-0000-0000-0000-000000000001",),
            solver_configuration=default_solver_configuration(max_deterministic_time_per_stage=6.0),
            environment_lock_hash="e" * 64,
            now=now,
        )
    assert reused_key.value.code == "IDEMPOTENCY_KEY_REUSED"
    with pytest.raises(ScenarioServiceError) as hidden:
        service.cancel(
            owner_user_id=secrets.token_hex(16),
            scenario_id=stored.scenario_id,
            now=now,
        )
    assert hidden.value.code == "SCENARIO_NOT_FOUND"
    engine.dispose()
