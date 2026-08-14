import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ratereplay_optimizer.models import (
    CandidateSchedule,
    CanonicalProfileSlot,
    ContiguousFixedShapeSpec,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ObjectiveTuple,
    OccurrenceSchedule,
    ReferenceSlot,
    ScenarioElectricalConstraints,
    ScenarioInput,
    ScheduleSlot,
)
from ratereplay_optimizer.verification import (
    ScheduleVerificationError,
    VerifiedSchedule,
    candidate_from_reference,
    select_strict_improvement,
    verify_candidate_schedule,
)
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.schema import AccountFacts

ROOT = Path(__file__).resolve().parents[3]
START = datetime(2026, 7, 1, 7, tzinfo=UTC)
LOAD_ID = UUID("00000000-0000-0000-0000-000000000001")
OCCURRENCE_ID = UUID("10000000-0000-0000-0000-000000000001")


def _account() -> AccountFacts:
    payload = json.loads(
        (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
    )["account_facts"]
    payload["qualifying_technologies"] = ()
    return AccountFacts.model_validate_json(json.dumps(payload))


def _profile(
    measured: tuple[int, ...] = (1_000, 1_000, 1_000, 1_000),
    *,
    duration_seconds: int = 3_600,
) -> tuple[CanonicalProfileSlot, ...]:
    return tuple(
        CanonicalProfileSlot(
            slot_start_utc=START + timedelta(seconds=index * duration_seconds),
            duration_seconds=duration_seconds,
            measured_energy_wh=amount,
        )
        for index, amount in enumerate(measured)
    )


def _reference(
    amounts: tuple[int, ...],
    *,
    duration_seconds: int = 3_600,
) -> tuple[ReferenceSlot, ...]:
    return tuple(
        ReferenceSlot(
            slot_start_utc=START + timedelta(seconds=index * duration_seconds),
            duration_seconds=duration_seconds,
            energy_wh=amount,
        )
        for index, amount in enumerate(amounts)
    )


def _scenario(
    *,
    reference: tuple[int, ...] = (0, 500, 500, 0),
    measured: tuple[int, ...] = (1_000, 1_000, 1_000, 1_000),
    start: datetime = START,
    deadline: datetime = START + timedelta(hours=4),
    minimum_power_w: int = 0,
    maximum_power_w: int = 2_000,
    constraints: ScenarioElectricalConstraints | None = None,
    duration_seconds: int = 3_600,
) -> ScenarioInput:
    occurrence = LoadOccurrence(
        occurrence_id=OCCURRENCE_ID,
        required_energy_wh=sum(reference),
        earliest_start_utc=start,
        deadline_utc=deadline,
        reference_schedule=_reference(reference, duration_seconds=duration_seconds),
    )
    load = FlexibleLoad(
        load_id=LOAD_ID,
        physical_asset_key="ev-1",
        kind="EV",
        mode="SHIFT_EXISTING",
        execution_spec=InterruptibleModulatingSpec(
            execution_type="INTERRUPTIBLE_MODULATING",
            maximum_power_w=maximum_power_w,
            minimum_power_when_active_w=minimum_power_w,
        ),
        occurrences=(occurrence,),
    )
    return ScenarioInput(
        scenario_version="historical-flex-scenario-v1",
        profile_content_sha256="a" * 64,
        tariff_version_id=compile_tariff(ROOT).ir.tariff_version_id,
        profile_slots=_profile(measured, duration_seconds=duration_seconds),
        loads=(load,),
        electrical_constraints=constraints or ScenarioElectricalConstraints(),
    )


def _candidate(scenario: ScenarioInput, amounts: tuple[int, ...]) -> CandidateSchedule:
    return CandidateSchedule(
        occurrences=(
            OccurrenceSchedule(
                occurrence_id=OCCURRENCE_ID,
                slots=tuple(
                    ScheduleSlot(
                        slot_start_utc=slot.slot_start_utc,
                        duration_seconds=slot.duration_seconds,
                        energy_wh=amounts[index],
                    )
                    for index, slot in enumerate(scenario.profile_slots)
                ),
            ),
        )
    )


def _verify(scenario: ScenarioInput, candidate: CandidateSchedule) -> VerifiedSchedule:
    return verify_candidate_schedule(scenario, candidate, compile_tariff(ROOT), _account())


def _assert_error(
    scenario: ScenarioInput,
    candidate: CandidateSchedule,
    code: str,
) -> ScheduleVerificationError:
    with pytest.raises(ScheduleVerificationError) as captured:
        _verify(scenario, candidate)
    assert captured.value.code == code
    return captured.value


def test_reference_candidate_is_freshly_billed_and_has_exact_objective_tuple() -> None:
    scenario = _scenario()
    first = _verify(scenario, candidate_from_reference(scenario))
    second = _verify(scenario, candidate_from_reference(scenario))

    assert first.record.objective == ObjectiveTuple(
        supported_cost_cents=first.billing_result.supported_calculated_cents,
        changed_occurrence_slot_count=0,
        completion_slot_index_sum=3,
        stable_slot_order_score=2_500,
    )
    assert tuple(slot.energy_wh for slot in first.record.candidate_profile) == (1_000,) * 4
    assert first.record.billing_result_sha256 == first.billing_result.result_sha256
    assert first.record.verification_sha256 == second.record.verification_sha256
    assert first.record.status == "VALID"


def test_shifted_candidate_replaces_only_declared_reference_energy() -> None:
    scenario = _scenario()
    verified = _verify(scenario, _candidate(scenario, (500, 500, 0, 0)))

    assert tuple(slot.energy_wh for slot in verified.record.candidate_profile) == (
        1_500,
        1_000,
        500,
        1_000,
    )
    assert verified.record.objective.changed_occurrence_slot_count == 2
    assert verified.record.objective.completion_slot_index_sum == 2
    assert verified.record.objective.stable_slot_order_score == 1_500


def test_occurrence_set_and_slot_vector_must_match_exactly() -> None:
    scenario = _scenario()
    candidate = candidate_from_reference(scenario)
    empty = candidate.model_copy(update={"occurrences": ()})
    _assert_error(scenario, empty, "VERIFIER_OCCURRENCE_SET_MISMATCH")

    shortened = candidate.occurrences[0].model_copy(
        update={"slots": candidate.occurrences[0].slots[:-1]}
    )
    _assert_error(
        scenario,
        candidate.model_copy(update={"occurrences": (shortened,)}),
        "VERIFIER_SLOT_VECTOR_MISMATCH",
    )

    shifted_slot = (
        candidate.occurrences[0]
        .slots[0]
        .model_copy(update={"slot_start_utc": START + timedelta(minutes=1)})
    )
    bad_slots = (shifted_slot, *candidate.occurrences[0].slots[1:])
    bad_identity = candidate.occurrences[0].model_copy(update={"slots": bad_slots})
    _assert_error(
        scenario,
        candidate.model_copy(update={"occurrences": (bad_identity,)}),
        "VERIFIER_SLOT_VECTOR_MISMATCH",
    )


def test_duplicate_candidate_occurrence_is_rejected() -> None:
    scenario = _scenario()
    candidate = candidate_from_reference(scenario)
    duplicated = candidate.model_copy(
        update={"occurrences": (candidate.occurrences[0], candidate.occurrences[0])}
    )
    _assert_error(scenario, duplicated, "VERIFIER_DUPLICATE_OCCURRENCE")


def test_corrupt_energy_window_and_power_each_fail_for_intended_reason() -> None:
    scenario = _scenario(
        start=START + timedelta(hours=1),
        deadline=START + timedelta(hours=3),
    )
    _assert_error(
        scenario,
        _candidate(scenario, (0, 500, 0, 0)),
        "VERIFIER_ENERGY_CONSERVATION_FAILED",
    )
    _assert_error(
        scenario,
        _candidate(scenario, (500, 500, 0, 0)),
        "VERIFIER_WINDOW_VIOLATION",
    )

    maximum = _scenario(maximum_power_w=500)
    _assert_error(
        maximum,
        _candidate(maximum, (0, 501, 499, 0)),
        "VERIFIER_MAXIMUM_POWER_VIOLATION",
    )
    minimum = _scenario(minimum_power_w=500)
    _assert_error(
        minimum,
        _candidate(minimum, (0, 499, 501, 0)),
        "VERIFIER_MINIMUM_POWER_VIOLATION",
    )


def test_fixed_shape_corruption_fails_independent_contiguity_check() -> None:
    scenario = _scenario(reference=(0, 250, 500, 0))
    occurrence = scenario.loads[0].occurrences[0]
    fixed = scenario.loads[0].model_copy(
        update={
            "kind": "DISHWASHER",
            "execution_spec": ContiguousFixedShapeSpec(
                execution_type="CONTIGUOUS_FIXED_SHAPE",
                fixed_slot_shape_wh=(250, 500),
            ),
        }
    )
    scenario = scenario.model_copy(update={"loads": (fixed,)})
    _verify(scenario, candidate_from_reference(scenario))

    corrupted = _candidate(scenario, (0, 500, 250, 0))
    error = _assert_error(scenario, corrupted, "VERIFIER_CONTIGUITY_VIOLATION")
    assert error.witness["occurrence_id"] == str(occurrence.occurrence_id)


def test_site_and_flexible_caps_are_independently_rechecked() -> None:
    site = _scenario(constraints=ScenarioElectricalConstraints(site_import_cap_w=1_100))
    _assert_error(
        site,
        _candidate(site, (500, 500, 0, 0)),
        "VERIFIER_SITE_IMPORT_CAP_VIOLATION",
    )

    flexible = _scenario(
        constraints=ScenarioElectricalConstraints(flexible_load_aggregate_cap_w=600)
    )
    _assert_error(
        flexible,
        _candidate(flexible, (0, 1_000, 0, 0)),
        "VERIFIER_FLEXIBLE_LOAD_CAP_VIOLATION",
    )


def test_claimed_cost_and_tariff_identity_are_fail_closed() -> None:
    scenario = _scenario()
    candidate = candidate_from_reference(scenario)
    valid = _verify(scenario, candidate)
    with pytest.raises(ScheduleVerificationError) as captured:
        verify_candidate_schedule(
            scenario,
            candidate,
            compile_tariff(ROOT),
            _account(),
            claimed_supported_cost_cents=(valid.billing_result.supported_calculated_cents + 1),
        )
    assert captured.value.code == "VERIFIER_COST_MISMATCH"

    wrong_tariff = scenario.model_copy(update={"tariff_version_id": "different-version"})
    _assert_error(wrong_tariff, candidate, "VERIFIER_TARIFF_VERSION_MISMATCH")


def test_verifier_refuses_profile_contract_corruption() -> None:
    scenario = _scenario(duration_seconds=7_200)
    _assert_error(
        scenario,
        candidate_from_reference(scenario),
        "VERIFIER_PROFILE_INVALID",
    )

    clean = _scenario()
    duplicate_asset = clean.model_copy(update={"loads": (clean.loads[0],) * 2})
    _assert_error(
        duplicate_asset,
        candidate_from_reference(clean),
        "VERIFIER_ASSET_IDENTITY_INVALID",
    )


def test_selector_requires_strict_complete_tuple_improvement() -> None:
    scenario = _scenario()
    reference = _verify(scenario, candidate_from_reference(scenario))

    equal = select_strict_improvement(reference, reference)
    assert equal.selected is reference
    assert equal.selected_source == "REFERENCE"
    assert equal.reason == "REFERENCE_EQUAL_OR_BETTER"

    no_incumbent = select_strict_improvement(None, reference)
    assert no_incumbent.selected is reference
    assert no_incumbent.reason == "NO_VERIFIED_INCUMBENT"

    objective = reference.record.objective.model_copy(
        update={
            "supported_cost_cents": reference.record.objective.supported_cost_cents - 1,
            "changed_occurrence_slot_count": 999,
        }
    )
    better = replace(reference, record=reference.record.model_copy(update={"objective": objective}))
    selected = select_strict_improvement(better, reference)
    assert selected.selected is better
    assert selected.selected_source == "SOLVER_INCUMBENT"
    assert selected.reason == "INCUMBENT_STRICTLY_BETTER"
