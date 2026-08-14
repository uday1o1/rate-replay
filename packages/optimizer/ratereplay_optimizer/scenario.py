"""Fail-closed historical scenario admission and exact profile decomposition."""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    ContiguousFixedShapeSpec,
    EnergySlot,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ReferenceValidationRecord,
    ScenarioDecomposition,
    ScenarioInput,
    ValidatedScenario,
)


class ScenarioValidationError(ValueError):
    def __init__(self, code: str, message: str, **witness: object) -> None:
        super().__init__(message)
        self.code = code
        self.witness = witness


def _fail(code: str, message: str, **witness: object) -> None:
    raise ScenarioValidationError(code, message, **witness)


def _validate_profile(slots: tuple[CanonicalProfileSlot, ...]) -> None:
    resolution = slots[0].duration_seconds
    for index, slot in enumerate(slots):
        if slot.duration_seconds != resolution:
            _fail(
                "PROFILE_RESOLUTION_MISMATCH",
                "Canonical profile slots must use one resolution",
                slot_index=index,
            )
    for index, (left, right) in enumerate(pairwise(slots)):
        expected = left.slot_start_utc + timedelta(seconds=left.duration_seconds)
        if right.slot_start_utc != expected:
            _fail(
                "PROFILE_SLOT_VECTOR_INVALID",
                "Canonical profile slots must be contiguous and ordered",
                slot_index=index + 1,
            )


def _validate_identities(loads: tuple[FlexibleLoad, ...]) -> None:
    load_ids = [load.load_id for load in loads]
    if len(load_ids) != len(set(load_ids)):
        _fail("DUPLICATE_LOAD_ID", "Scenario load identifiers must be unique")
    asset_keys = [load.physical_asset_key for load in loads]
    if len(asset_keys) != len(set(asset_keys)):
        _fail(
            "DUPLICATE_PHYSICAL_ASSET_KEY",
            "One physical asset key cannot identify multiple loads",
        )
    occurrence_ids = [occurrence.occurrence_id for load in loads for occurrence in load.occurrences]
    if len(occurrence_ids) != len(set(occurrence_ids)):
        _fail("DUPLICATE_OCCURRENCE_ID", "Occurrence identifiers must be unique")


def _validate_disjoint_occurrences(load: FlexibleLoad) -> None:
    ordered = sorted(
        load.occurrences,
        key=lambda occurrence: (occurrence.earliest_start_utc, occurrence.deadline_utc),
    )
    for left, right in pairwise(ordered):
        if right.earliest_start_utc < left.deadline_utc:
            _fail(
                "OVERLAPPING_LOAD_OCCURRENCES",
                "One physical load has overlapping half-open occurrence windows",
                load_id=str(load.load_id),
                first_occurrence_id=str(left.occurrence_id),
                second_occurrence_id=str(right.occurrence_id),
            )


def _aligned_window_indices(
    occurrence: LoadOccurrence,
    slots: tuple[CanonicalProfileSlot, ...],
) -> tuple[int, int]:
    boundaries = {slot.slot_start_utc: index for index, slot in enumerate(slots)}
    final_end = slots[-1].slot_start_utc + timedelta(seconds=slots[-1].duration_seconds)
    boundaries[final_end] = len(slots)
    if occurrence.earliest_start_utc not in boundaries:
        _fail(
            "NON_ALIGNED_OCCURRENCE_BOUNDARY",
            "Occurrence start is not a canonical profile boundary",
            occurrence_id=str(occurrence.occurrence_id),
            endpoint="earliest_start_utc",
        )
    if occurrence.deadline_utc not in boundaries:
        _fail(
            "NON_ALIGNED_OCCURRENCE_BOUNDARY",
            "Occurrence deadline is not a canonical profile boundary",
            occurrence_id=str(occurrence.occurrence_id),
            endpoint="deadline_utc",
        )
    start = boundaries[occurrence.earliest_start_utc]
    end = boundaries[occurrence.deadline_utc]
    if start >= end:
        _fail(
            "OCCURRENCE_OUTSIDE_PROFILE",
            "Occurrence must remain inside the admitted profile window",
            occurrence_id=str(occurrence.occurrence_id),
        )
    return start, end


def _reference_energy(
    occurrence: LoadOccurrence,
    slots: tuple[CanonicalProfileSlot, ...],
) -> tuple[int, ...]:
    if len(occurrence.reference_schedule) != len(slots):
        _fail(
            "REFERENCE_SLOT_VECTOR_MISMATCH",
            "Reference schedule must contain every canonical profile slot",
            occurrence_id=str(occurrence.occurrence_id),
        )
    energy: list[int] = []
    for index, (reference, profile) in enumerate(
        zip(occurrence.reference_schedule, slots, strict=True)
    ):
        if (
            reference.slot_start_utc != profile.slot_start_utc
            or reference.duration_seconds != profile.duration_seconds
        ):
            _fail(
                "REFERENCE_SLOT_VECTOR_MISMATCH",
                "Reference schedule slot identity differs from the canonical profile",
                occurrence_id=str(occurrence.occurrence_id),
                slot_index=index,
            )
        energy.append(reference.energy_wh)
    if sum(energy) != occurrence.required_energy_wh:
        _fail(
            "REFERENCE_ENERGY_MISMATCH",
            "Reference schedule energy differs from the occurrence requirement",
            occurrence_id=str(occurrence.occurrence_id),
        )
    return tuple(energy)


def _validate_interruptible(
    spec: InterruptibleModulatingSpec,
    occurrence: LoadOccurrence,
    energy: tuple[int, ...],
    slots: tuple[CanonicalProfileSlot, ...],
    start: int,
    end: int,
) -> None:
    for index, (amount, slot) in enumerate(zip(energy, slots, strict=True)):
        if amount > 0 and not start <= index < end:
            _fail(
                "REFERENCE_ENERGY_OUTSIDE_WINDOW",
                "Reference energy appears outside the half-open occurrence window",
                occurrence_id=str(occurrence.occurrence_id),
                slot_index=index,
            )
        if amount * 3_600 > spec.maximum_power_w * slot.duration_seconds:
            _fail(
                "REFERENCE_MAXIMUM_POWER_EXCEEDED",
                "Reference slot exceeds the declared maximum average power",
                occurrence_id=str(occurrence.occurrence_id),
                slot_index=index,
            )
        if amount > 0 and amount * 3_600 < spec.minimum_power_when_active_w * slot.duration_seconds:
            _fail(
                "REFERENCE_MINIMUM_POWER_VIOLATED",
                "Positive reference slot is below the declared active minimum power",
                occurrence_id=str(occurrence.occurrence_id),
                slot_index=index,
            )


def _validate_fixed_shape(
    spec: ContiguousFixedShapeSpec,
    occurrence: LoadOccurrence,
    energy: tuple[int, ...],
    start: int,
    end: int,
) -> None:
    if sum(spec.fixed_slot_shape_wh) != occurrence.required_energy_wh:
        _fail(
            "FIXED_SHAPE_ENERGY_MISMATCH",
            "Fixed shape energy differs from the occurrence requirement",
            occurrence_id=str(occurrence.occurrence_id),
        )
    shape_length = len(spec.fixed_slot_shape_wh)
    matched_start: int | None = None
    for candidate_start in range(start, end - shape_length + 1):
        candidate = [0] * len(energy)
        candidate[candidate_start : candidate_start + shape_length] = spec.fixed_slot_shape_wh
        if tuple(candidate) == energy:
            matched_start = candidate_start
            break
    if matched_start is None:
        _fail(
            "FIXED_SHAPE_REFERENCE_MISMATCH",
            "Reference schedule does not contain the exact fixed shape at an allowed start",
            occurrence_id=str(occurrence.occurrence_id),
        )


def _validate_occurrence(
    load: FlexibleLoad,
    occurrence: LoadOccurrence,
    slots: tuple[CanonicalProfileSlot, ...],
) -> tuple[int, ...]:
    start, end = _aligned_window_indices(occurrence, slots)
    energy = _reference_energy(occurrence, slots)
    spec = load.execution_spec
    if isinstance(spec, InterruptibleModulatingSpec):
        _validate_interruptible(spec, occurrence, energy, slots, start, end)
    else:
        _validate_fixed_shape(spec, occurrence, energy, start, end)
    return energy


def _energy_slots(
    slots: tuple[CanonicalProfileSlot, ...], energy_wh: list[int]
) -> tuple[EnergySlot, ...]:
    return tuple(
        EnergySlot(
            slot_start_utc=slot.slot_start_utc,
            duration_seconds=slot.duration_seconds,
            energy_wh=energy,
        )
        for slot, energy in zip(slots, energy_wh, strict=True)
    )


def _validate_caps(
    scenario: ScenarioInput,
    unchanged: list[int],
    flexible: list[int],
) -> None:
    site_cap = scenario.electrical_constraints.site_import_cap_w
    flexible_cap = scenario.electrical_constraints.flexible_load_aggregate_cap_w
    for index, slot in enumerate(scenario.profile_slots):
        if site_cap is not None and unchanged[index] * 3_600 > site_cap * slot.duration_seconds:
            _fail(
                "REFERENCE_SITE_IMPORT_CAP_EXCEEDED",
                "Unchanged reference profile exceeds the site average-power cap",
                slot_index=index,
            )
        if (
            flexible_cap is not None
            and flexible[index] * 3_600 > flexible_cap * slot.duration_seconds
        ):
            _fail(
                "REFERENCE_FLEXIBLE_LOAD_CAP_EXCEEDED",
                "Reference flexible energy exceeds the aggregate average-power cap",
                slot_index=index,
            )


def validate_and_decompose_scenario(scenario: ScenarioInput) -> ValidatedScenario:
    """Validate a complete reference before any solver or job construction."""

    slots = scenario.profile_slots
    _validate_profile(slots)
    _validate_identities(scenario.loads)
    for load in scenario.loads:
        _validate_disjoint_occurrences(load)

    shift_reference = [0] * len(slots)
    addition_reference = [0] * len(slots)
    occurrence_count = 0
    for load in scenario.loads:
        for occurrence in load.occurrences:
            occurrence_count += 1
            energy = _validate_occurrence(load, occurrence, slots)
            target = shift_reference if load.mode == "SHIFT_EXISTING" else addition_reference
            for index, amount in enumerate(energy):
                target[index] += amount

    background = [
        slot.measured_energy_wh - shift_reference[index] for index, slot in enumerate(slots)
    ]
    for index, amount in enumerate(background):
        if amount < 0:
            _fail(
                "NEGATIVE_FIXED_BACKGROUND",
                "Existing flexible references exceed measured profile energy",
                slot_index=index,
            )
    reconstructed_measured = [
        background[index] + shift_reference[index] for index in range(len(slots))
    ]
    if reconstructed_measured != [slot.measured_energy_wh for slot in slots]:
        _fail("MEASURED_RECONSTRUCTION_MISMATCH", "Existing-load reconstruction is not exact")
    flexible_reference = [
        shift_reference[index] + addition_reference[index] for index in range(len(slots))
    ]
    unchanged = [background[index] + flexible_reference[index] for index in range(len(slots))]
    _validate_caps(scenario, unchanged, flexible_reference)

    checked_codes = (
        "PROFILE_SLOT_VECTOR_INVALID",
        "DUPLICATE_PHYSICAL_ASSET_KEY",
        "OVERLAPPING_LOAD_OCCURRENCES",
        "NON_ALIGNED_OCCURRENCE_BOUNDARY",
        "REFERENCE_SLOT_VECTOR_MISMATCH",
        "REFERENCE_ENERGY_MISMATCH",
        "REFERENCE_EXECUTION_SPEC",
        "NEGATIVE_FIXED_BACKGROUND",
        "REFERENCE_SITE_IMPORT_CAP_EXCEEDED",
        "REFERENCE_FLEXIBLE_LOAD_CAP_EXCEEDED",
    )
    decomposition = ScenarioDecomposition(
        fixed_background=_energy_slots(slots, background),
        shift_existing_reference=_energy_slots(slots, shift_reference),
        historical_addition_reference=_energy_slots(slots, addition_reference),
        reconstructed_measured_profile=_energy_slots(slots, reconstructed_measured),
        unchanged_reference_profile=_energy_slots(slots, unchanged),
        exact_measured_reconstruction=True,
    )
    return ValidatedScenario(
        scenario=scenario,
        decomposition=decomposition,
        reference_validation=ReferenceValidationRecord(
            load_count=len(scenario.loads),
            occurrence_count=occurrence_count,
            slot_count=len(slots),
            checked_constraint_codes=checked_codes,
        ),
    )
