from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError
from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    ContiguousFixedShapeSpec,
    EnergySlot,
    FlexibleLoad,
    LoadOccurrence,
    ReferenceSlot,
    ScenarioElectricalConstraints,
    ScenarioInput,
)
from ratereplay_optimizer.scenario import (
    ScenarioValidationError,
    validate_and_decompose_scenario,
)

START = datetime(2026, 7, 1, tzinfo=UTC)
PROFILE_HASH = "a" * 64
LOAD_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_LOAD_ID = UUID("00000000-0000-0000-0000-000000000002")
OCCURRENCE_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_OCCURRENCE_ID = UUID("10000000-0000-0000-0000-000000000002")


def _profile(
    measured: tuple[int, ...] = (1_000, 1_000, 1_000, 1_000),
) -> tuple[CanonicalProfileSlot, ...]:
    return tuple(
        CanonicalProfileSlot(
            slot_start_utc=START + timedelta(hours=index),
            duration_seconds=3_600,
            measured_energy_wh=amount,
        )
        for index, amount in enumerate(measured)
    )


def _reference(amounts: tuple[int, ...], *, duration: int = 3_600) -> tuple[ReferenceSlot, ...]:
    return tuple(
        ReferenceSlot(
            slot_start_utc=START + timedelta(hours=index),
            duration_seconds=duration,
            energy_wh=amount,
        )
        for index, amount in enumerate(amounts)
    )


def _occurrence(
    amounts: tuple[int, ...] = (0, 500, 500, 0),
    *,
    occurrence_id: UUID = OCCURRENCE_ID,
    start: datetime = START,
    deadline: datetime = START + timedelta(hours=4),
) -> LoadOccurrence:
    return LoadOccurrence(
        occurrence_id=occurrence_id,
        required_energy_wh=sum(amounts),
        earliest_start_utc=start,
        deadline_utc=deadline,
        reference_schedule=_reference(amounts),
    )


def _interruptible_load(
    occurrence: LoadOccurrence | None = None,
    *,
    load_id: UUID = LOAD_ID,
    asset_key: str = "ev-1",
    mode: str = "SHIFT_EXISTING",
    minimum_power_w: int = 0,
    maximum_power_w: int = 2_000,
    occurrences: tuple[LoadOccurrence, ...] | None = None,
) -> FlexibleLoad:
    selected = occurrences if occurrences is not None else (occurrence or _occurrence(),)
    return FlexibleLoad.model_validate(
        {
            "load_id": load_id,
            "physical_asset_key": asset_key,
            "kind": "EV",
            "mode": mode,
            "execution_spec": {
                "execution_type": "INTERRUPTIBLE_MODULATING",
                "maximum_power_w": maximum_power_w,
                "minimum_power_when_active_w": minimum_power_w,
            },
            "occurrences": selected,
        },
        strict=True,
    )


def _scenario(
    *loads: FlexibleLoad,
    measured: tuple[int, ...] = (1_000, 1_000, 1_000, 1_000),
    constraints: ScenarioElectricalConstraints | None = None,
) -> ScenarioInput:
    return ScenarioInput(
        scenario_version="historical-flex-scenario-v1",
        profile_content_sha256=PROFILE_HASH,
        tariff_version_id="pge-e1-2026-03-01",
        profile_slots=_profile(measured),
        loads=loads or (_interruptible_load(),),
        electrical_constraints=constraints or ScenarioElectricalConstraints(),
    )


def _energy(series: tuple[EnergySlot, ...]) -> tuple[int, ...]:
    return tuple(slot.energy_wh for slot in series)


def _assert_error(scenario: ScenarioInput, code: str) -> ScenarioValidationError:
    with pytest.raises(ScenarioValidationError) as captured:
        validate_and_decompose_scenario(scenario)
    assert captured.value.code == code
    return captured.value


def test_shift_existing_decomposition_exactly_reconstructs_measurement() -> None:
    validated = validate_and_decompose_scenario(_scenario())

    assert _energy(validated.decomposition.fixed_background) == (1_000, 500, 500, 1_000)
    assert _energy(validated.decomposition.shift_existing_reference) == (0, 500, 500, 0)
    assert _energy(validated.decomposition.historical_addition_reference) == (0, 0, 0, 0)
    assert _energy(validated.decomposition.reconstructed_measured_profile) == (
        1_000,
        1_000,
        1_000,
        1_000,
    )
    assert _energy(validated.decomposition.unchanged_reference_profile) == (
        1_000,
        1_000,
        1_000,
        1_000,
    )
    assert validated.decomposition.exact_measured_reconstruction is True


def test_historical_addition_is_counted_once_without_changing_background() -> None:
    load = _interruptible_load(mode="HISTORICAL_ADDITION")
    validated = validate_and_decompose_scenario(_scenario(load))

    assert _energy(validated.decomposition.fixed_background) == (1_000,) * 4
    assert _energy(validated.decomposition.reconstructed_measured_profile) == (1_000,) * 4
    assert _energy(validated.decomposition.unchanged_reference_profile) == (
        1_000,
        1_500,
        1_500,
        1_000,
    )
    assert (
        validated.decomposition.historical_addition_label
        == "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"
    )


def test_overlapping_same_asset_windows_fail_before_reference_validation() -> None:
    first = _occurrence(
        (500, 0, 0, 0),
        start=START,
        deadline=START + timedelta(hours=2),
    )
    second = _occurrence(
        (0, 0, 500, 0),
        occurrence_id=OTHER_OCCURRENCE_ID,
        start=START + timedelta(hours=1),
        deadline=START + timedelta(hours=3),
    ).model_copy(update={"reference_schedule": _reference((0, 500, 0))})
    load = _interruptible_load(occurrences=(first, second))

    error = _assert_error(_scenario(load), "OVERLAPPING_LOAD_OCCURRENCES")
    assert error.witness["first_occurrence_id"] == str(OCCURRENCE_ID)
    assert error.witness["second_occurrence_id"] == str(OTHER_OCCURRENCE_ID)


def test_adjacent_half_open_occurrence_windows_are_disjoint() -> None:
    first = _occurrence(
        (500, 0, 0, 0),
        start=START,
        deadline=START + timedelta(hours=1),
    )
    second = _occurrence(
        (0, 500, 0, 0),
        occurrence_id=OTHER_OCCURRENCE_ID,
        start=START + timedelta(hours=1),
        deadline=START + timedelta(hours=2),
    )
    load = _interruptible_load(occurrences=(first, second))

    validated = validate_and_decompose_scenario(_scenario(load))
    assert validated.reference_validation.occurrence_count == 2


def test_duplicate_physical_asset_identity_is_rejected() -> None:
    other = _interruptible_load(
        _occurrence(occurrence_id=OTHER_OCCURRENCE_ID),
        load_id=OTHER_LOAD_ID,
        asset_key="ev-1",
    )
    _assert_error(_scenario(_interruptible_load(), other), "DUPLICATE_PHYSICAL_ASSET_KEY")


@pytest.mark.parametrize("endpoint", ["start", "deadline"])
def test_non_aligned_occurrence_boundary_is_rejected(endpoint: str) -> None:
    updates = {
        "start": {"earliest_start_utc": START + timedelta(minutes=30)},
        "deadline": {"deadline_utc": START + timedelta(hours=3, minutes=30)},
    }
    occurrence = _occurrence().model_copy(update=updates[endpoint])

    error = _assert_error(
        _scenario(_interruptible_load(occurrence)),
        "NON_ALIGNED_OCCURRENCE_BOUNDARY",
    )
    expected_endpoint = "earliest_start_utc" if endpoint == "start" else "deadline_utc"
    assert error.witness["endpoint"] == expected_endpoint


@pytest.mark.parametrize("defect", ["length", "start", "duration"])
def test_reference_must_match_complete_canonical_slot_vector(defect: str) -> None:
    schedule = list(_reference((0, 500, 500, 0)))
    if defect == "length":
        schedule.pop()
    elif defect == "start":
        schedule[1] = schedule[1].model_copy(
            update={"slot_start_utc": START + timedelta(minutes=90)}
        )
    else:
        schedule[1] = schedule[1].model_copy(update={"duration_seconds": 1_800})
    occurrence = _occurrence().model_copy(update={"reference_schedule": tuple(schedule)})

    _assert_error(
        _scenario(_interruptible_load(occurrence)),
        "REFERENCE_SLOT_VECTOR_MISMATCH",
    )


def test_reference_total_must_equal_occurrence_energy() -> None:
    occurrence = _occurrence().model_copy(update={"required_energy_wh": 1_001})
    _assert_error(_scenario(_interruptible_load(occurrence)), "REFERENCE_ENERGY_MISMATCH")


def test_interruptible_reference_must_stay_inside_window() -> None:
    occurrence = _occurrence(
        (500, 500, 0, 0),
        start=START + timedelta(hours=1),
        deadline=START + timedelta(hours=3),
    )
    _assert_error(
        _scenario(_interruptible_load(occurrence)),
        "REFERENCE_ENERGY_OUTSIDE_WINDOW",
    )


def test_interruptible_reference_obeys_maximum_and_active_minimum_power() -> None:
    high = _occurrence((0, 501, 499, 0))
    _assert_error(
        _scenario(_interruptible_load(high, maximum_power_w=500)),
        "REFERENCE_MAXIMUM_POWER_EXCEEDED",
    )

    low = _occurrence((0, 499, 501, 0))
    _assert_error(
        _scenario(_interruptible_load(low, minimum_power_w=500)),
        "REFERENCE_MINIMUM_POWER_VIOLATED",
    )


def test_contiguous_fixed_shape_reference_must_match_exact_shape() -> None:
    occurrence = _occurrence(
        (0, 250, 500, 0),
        start=START + timedelta(hours=1),
        deadline=START + timedelta(hours=3),
    )
    load = FlexibleLoad(
        load_id=LOAD_ID,
        physical_asset_key="dishwasher-1",
        kind="DISHWASHER",
        mode="SHIFT_EXISTING",
        execution_spec=ContiguousFixedShapeSpec(
            execution_type="CONTIGUOUS_FIXED_SHAPE",
            fixed_slot_shape_wh=(250, 500),
        ),
        occurrences=(occurrence,),
    )
    validated = validate_and_decompose_scenario(_scenario(load))
    assert _energy(validated.decomposition.shift_existing_reference) == (0, 250, 500, 0)

    wrong = occurrence.model_copy(update={"reference_schedule": _reference((0, 500, 250, 0))})
    _assert_error(
        _scenario(load.model_copy(update={"occurrences": (wrong,)})),
        "FIXED_SHAPE_REFERENCE_MISMATCH",
    )


def test_fixed_shape_energy_must_equal_occurrence_requirement() -> None:
    occurrence = _occurrence((0, 250, 500, 0))
    load = FlexibleLoad(
        load_id=LOAD_ID,
        physical_asset_key="dishwasher-1",
        kind="DISHWASHER",
        mode="SHIFT_EXISTING",
        execution_spec=ContiguousFixedShapeSpec(
            execution_type="CONTIGUOUS_FIXED_SHAPE",
            fixed_slot_shape_wh=(250, 499),
        ),
        occurrences=(occurrence,),
    )
    _assert_error(_scenario(load), "FIXED_SHAPE_ENERGY_MISMATCH")


def test_existing_reference_cannot_make_fixed_background_negative() -> None:
    load = _interruptible_load(_occurrence((0, 1_001, 0, 0)))
    _assert_error(_scenario(load), "NEGATIVE_FIXED_BACKGROUND")


def test_reference_must_obey_site_and_flexible_aggregate_caps() -> None:
    historical = _interruptible_load(mode="HISTORICAL_ADDITION")
    site_constraints = ScenarioElectricalConstraints(site_import_cap_w=1_499)
    _assert_error(
        _scenario(historical, constraints=site_constraints),
        "REFERENCE_SITE_IMPORT_CAP_EXCEEDED",
    )

    flex_constraints = ScenarioElectricalConstraints(flexible_load_aggregate_cap_w=499)
    _assert_error(
        _scenario(historical, constraints=flex_constraints),
        "REFERENCE_FLEXIBLE_LOAD_CAP_EXCEEDED",
    )


def test_models_reject_non_utc_instants_and_unsupported_execution_types() -> None:
    with pytest.raises(ValidationError, match="UTC instant"):
        CanonicalProfileSlot(
            slot_start_utc=datetime(2026, 7, 1),
            duration_seconds=3_600,
            measured_energy_wh=1,
        )

    payload = _interruptible_load().model_dump()
    payload["execution_spec"] = {"execution_type": "UNSUPPORTED"}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        FlexibleLoad.model_validate(payload, strict=True)


@given(
    background=st.lists(st.integers(min_value=0, max_value=500), min_size=4, max_size=4),
    shifted=st.lists(st.integers(min_value=0, max_value=500), min_size=4, max_size=4),
    added=st.lists(st.integers(min_value=0, max_value=500), min_size=4, max_size=4),
)
def test_decomposition_property_never_double_counts_historical_additions(
    background: list[int], shifted: list[int], added: list[int]
) -> None:
    assume(sum(shifted) > 0)
    assume(sum(added) > 0)
    shifted_tuple = tuple(shifted)
    added_tuple = tuple(added)
    existing = _interruptible_load(
        _occurrence(shifted_tuple),
        maximum_power_w=500,
    )
    historical = _interruptible_load(
        _occurrence(added_tuple, occurrence_id=OTHER_OCCURRENCE_ID),
        load_id=OTHER_LOAD_ID,
        asset_key="ev-2",
        mode="HISTORICAL_ADDITION",
        maximum_power_w=500,
    )
    measured = tuple(background[index] + shifted[index] for index in range(4))

    decomposition = validate_and_decompose_scenario(
        _scenario(existing, historical, measured=measured)
    ).decomposition

    assert _energy(decomposition.fixed_background) == tuple(background)
    assert _energy(decomposition.reconstructed_measured_profile) == measured
    assert _energy(decomposition.unchanged_reference_profile) == tuple(
        background[index] + shifted[index] + added[index] for index in range(4)
    )


def test_reference_validation_record_explains_pre_solver_checks() -> None:
    validated = validate_and_decompose_scenario(_scenario())

    assert validated.reference_validation.status == "VALID"
    assert validated.reference_validation.load_count == 1
    assert validated.reference_validation.occurrence_count == 1
    assert validated.reference_validation.slot_count == 4
    assert "OVERLAPPING_LOAD_OCCURRENCES" in (
        validated.reference_validation.checked_constraint_codes
    )
    assert "NON_ALIGNED_OCCURRENCE_BOUNDARY" in (
        validated.reference_validation.checked_constraint_codes
    )
    assert all(
        slot.slot_start_utc in {START + timedelta(hours=index) for index in range(4)}
        for slot in validated.decomposition.unchanged_reference_profile
    )
