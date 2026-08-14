"""Independent schedule verification and reference billing recomputation."""

from __future__ import annotations

from calendar import timegm
from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from typing import Literal
from uuid import UUID

from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    ReplayResult,
    replay_compiled_tariff,
)
from ratereplay_tariffs.compiled import CompilationBundle
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

from ratereplay_optimizer.models import (
    CandidateProfileSlot,
    CandidateSchedule,
    CanonicalProfileSlot,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ObjectiveTuple,
    OccurrenceSchedule,
    ScenarioInput,
    ScheduleSlot,
    VerificationRecord,
)


class ScheduleVerificationError(ValueError):
    def __init__(self, code: str, message: str, **witness: object) -> None:
        super().__init__(message)
        self.code = code
        self.witness = witness


def _fail(code: str, message: str, **witness: object) -> None:
    raise ScheduleVerificationError(code, message, **witness)


@dataclass(frozen=True, slots=True)
class VerifiedSchedule:
    schedule: CandidateSchedule
    record: VerificationRecord
    billing_result: ReplayResult


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    selected: VerifiedSchedule
    incumbent: VerifiedSchedule | None
    reference: VerifiedSchedule
    selected_source: Literal["SOLVER_INCUMBENT", "REFERENCE"]
    reason: Literal[
        "INCUMBENT_STRICTLY_BETTER",
        "REFERENCE_EQUAL_OR_BETTER",
        "NO_VERIFIED_INCUMBENT",
    ]


def _canonical_occurrences(
    scenario: ScenarioInput,
) -> tuple[tuple[FlexibleLoad, LoadOccurrence], ...]:
    values = tuple((load, occurrence) for load in scenario.loads for occurrence in load.occurrences)
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item[1].deadline_utc,
                item[1].earliest_start_utc,
                item[0].load_id.bytes,
                item[1].occurrence_id.bytes,
            ),
        )
    )


def _profile_boundaries(slots: tuple[CanonicalProfileSlot, ...]) -> dict[object, int]:
    if not slots:
        _fail("VERIFIER_PROFILE_INVALID", "The canonical profile is empty")
    resolution = slots[0].duration_seconds
    boundaries: dict[object, int] = {}
    for index, slot in enumerate(slots):
        if slot.duration_seconds != resolution or slot.duration_seconds > 3_600:
            _fail(
                "VERIFIER_PROFILE_INVALID",
                "Canonical slots must use one supported duration of at most one hour",
                slot_index=index,
            )
        if slot.slot_start_utc.microsecond:
            _fail(
                "VERIFIER_PROFILE_INVALID",
                "Canonical slots must align to whole UTC seconds",
                slot_index=index,
            )
        boundaries[slot.slot_start_utc] = index
        if index:
            previous = slots[index - 1]
            expected = previous.slot_start_utc + timedelta(seconds=previous.duration_seconds)
            if slot.slot_start_utc != expected:
                _fail(
                    "VERIFIER_PROFILE_INVALID",
                    "Canonical slots must be contiguous and ordered",
                    slot_index=index,
                )
    final_end = slots[-1].slot_start_utc + timedelta(seconds=slots[-1].duration_seconds)
    boundaries[final_end] = len(slots)
    return boundaries


def _schedule_map(
    schedule: CandidateSchedule,
    expected_ids: set[UUID],
) -> dict[UUID, OccurrenceSchedule]:
    values: dict[UUID, OccurrenceSchedule] = {}
    for occurrence in schedule.occurrences:
        if occurrence.occurrence_id in values:
            _fail(
                "VERIFIER_DUPLICATE_OCCURRENCE",
                "Candidate contains a duplicate occurrence schedule",
                occurrence_id=str(occurrence.occurrence_id),
            )
        values[occurrence.occurrence_id] = occurrence
    if set(values) != expected_ids:
        _fail(
            "VERIFIER_OCCURRENCE_SET_MISMATCH",
            "Candidate occurrence identifiers do not exactly match the scenario",
            missing=tuple(sorted(str(value) for value in expected_ids - set(values))),
            unexpected=tuple(sorted(str(value) for value in set(values) - expected_ids)),
        )
    return values


def _energy_vector(
    occurrence_schedule: OccurrenceSchedule,
    slots: tuple[CanonicalProfileSlot, ...],
) -> tuple[int, ...]:
    if len(occurrence_schedule.slots) != len(slots):
        _fail(
            "VERIFIER_SLOT_VECTOR_MISMATCH",
            "Candidate must contain every canonical profile slot",
            occurrence_id=str(occurrence_schedule.occurrence_id),
        )
    values: list[int] = []
    for index, (candidate, canonical) in enumerate(
        zip(occurrence_schedule.slots, slots, strict=True)
    ):
        if (
            candidate.slot_start_utc != canonical.slot_start_utc
            or candidate.duration_seconds != canonical.duration_seconds
        ):
            _fail(
                "VERIFIER_SLOT_VECTOR_MISMATCH",
                "Candidate slot identity differs from the canonical profile",
                occurrence_id=str(occurrence_schedule.occurrence_id),
                slot_index=index,
            )
        values.append(candidate.energy_wh)
    return tuple(values)


def _reference_vector(
    occurrence: LoadOccurrence,
    slots: tuple[CanonicalProfileSlot, ...],
) -> tuple[int, ...]:
    if len(occurrence.reference_schedule) != len(slots):
        _fail(
            "VERIFIER_REFERENCE_INVALID",
            "Reference slot count differs from the canonical profile",
            occurrence_id=str(occurrence.occurrence_id),
        )
    values: list[int] = []
    for reference, canonical in zip(occurrence.reference_schedule, slots, strict=True):
        if (
            reference.slot_start_utc != canonical.slot_start_utc
            or reference.duration_seconds != canonical.duration_seconds
        ):
            _fail(
                "VERIFIER_REFERENCE_INVALID",
                "Reference slot identity differs from the canonical profile",
                occurrence_id=str(occurrence.occurrence_id),
            )
        values.append(reference.energy_wh)
    return tuple(values)


def _window_indices(
    occurrence: LoadOccurrence,
    boundaries: dict[object, int],
) -> tuple[int, int]:
    if occurrence.earliest_start_utc not in boundaries or occurrence.deadline_utc not in boundaries:
        _fail(
            "VERIFIER_NON_ALIGNED_OCCURRENCE_BOUNDARY",
            "Occurrence endpoints must match canonical profile boundaries",
            occurrence_id=str(occurrence.occurrence_id),
        )
    start = boundaries[occurrence.earliest_start_utc]
    end = boundaries[occurrence.deadline_utc]
    if start >= end:
        _fail(
            "VERIFIER_OCCURRENCE_OUTSIDE_PROFILE",
            "Occurrence must be nonempty and contained in the profile",
            occurrence_id=str(occurrence.occurrence_id),
        )
    return start, end


def _verify_disjoint_windows(scenario: ScenarioInput) -> None:
    load_ids: set[UUID] = set()
    asset_keys: set[str] = set()
    occurrence_ids: set[UUID] = set()
    for load in scenario.loads:
        if load.load_id in load_ids or load.physical_asset_key in asset_keys:
            _fail(
                "VERIFIER_ASSET_IDENTITY_INVALID",
                "Load and physical asset identities must be unique",
            )
        load_ids.add(load.load_id)
        asset_keys.add(load.physical_asset_key)
        for occurrence in load.occurrences:
            if occurrence.occurrence_id in occurrence_ids:
                _fail(
                    "VERIFIER_ASSET_IDENTITY_INVALID",
                    "Occurrence identities must be unique",
                )
            occurrence_ids.add(occurrence.occurrence_id)
        ordered = sorted(load.occurrences, key=lambda item: item.earliest_start_utc)
        for left, right in pairwise(ordered):
            if right.earliest_start_utc < left.deadline_utc:
                _fail(
                    "VERIFIER_OVERLAPPING_OCCURRENCES",
                    "One physical load has overlapping occurrence windows",
                    load_id=str(load.load_id),
                )


def _verify_execution(
    load: FlexibleLoad,
    occurrence: LoadOccurrence,
    energy: tuple[int, ...],
    slots: tuple[CanonicalProfileSlot, ...],
    start: int,
    end: int,
) -> None:
    if sum(energy) != occurrence.required_energy_wh:
        _fail(
            "VERIFIER_ENERGY_CONSERVATION_FAILED",
            "Candidate energy does not equal the occurrence requirement",
            occurrence_id=str(occurrence.occurrence_id),
        )
    spec = load.execution_spec
    if isinstance(spec, InterruptibleModulatingSpec):
        for index, (amount, slot) in enumerate(zip(energy, slots, strict=True)):
            if amount > 0 and not start <= index < end:
                _fail(
                    "VERIFIER_WINDOW_VIOLATION",
                    "Candidate energy appears outside the occurrence window",
                    occurrence_id=str(occurrence.occurrence_id),
                    slot_index=index,
                )
            if amount * 3_600 > spec.maximum_power_w * slot.duration_seconds:
                _fail(
                    "VERIFIER_MAXIMUM_POWER_VIOLATION",
                    "Candidate energy exceeds the exact maximum average power",
                    occurrence_id=str(occurrence.occurrence_id),
                    slot_index=index,
                )
            if (
                amount > 0
                and amount * 3_600 < spec.minimum_power_when_active_w * slot.duration_seconds
            ):
                _fail(
                    "VERIFIER_MINIMUM_POWER_VIOLATION",
                    "Candidate energy is below the exact active minimum power",
                    occurrence_id=str(occurrence.occurrence_id),
                    slot_index=index,
                )
        return
    shape = spec.fixed_slot_shape_wh
    if sum(shape) != occurrence.required_energy_wh:
        _fail(
            "VERIFIER_FIXED_SHAPE_ENERGY_MISMATCH",
            "Fixed shape energy differs from the occurrence requirement",
            occurrence_id=str(occurrence.occurrence_id),
        )
    for candidate_start in range(start, end - len(shape) + 1):
        expected = [0] * len(slots)
        expected[candidate_start : candidate_start + len(shape)] = shape
        if tuple(expected) == energy:
            return
    _fail(
        "VERIFIER_CONTIGUITY_VIOLATION",
        "Candidate does not contain the exact fixed shape at an allowed start",
        occurrence_id=str(occurrence.occurrence_id),
    )


def _background_and_reference(
    scenario: ScenarioInput,
) -> tuple[list[int], dict[UUID, tuple[int, ...]]]:
    slots = scenario.profile_slots
    shift = [0] * len(slots)
    references: dict[UUID, tuple[int, ...]] = {}
    for load, occurrence in _canonical_occurrences(scenario):
        reference = _reference_vector(occurrence, slots)
        references[occurrence.occurrence_id] = reference
        if load.mode == "SHIFT_EXISTING":
            for index, amount in enumerate(reference):
                shift[index] += amount
    background = [slot.measured_energy_wh - shift[index] for index, slot in enumerate(slots)]
    if any(amount < 0 for amount in background):
        _fail(
            "VERIFIER_DECOMPOSITION_FAILED",
            "Reference subtraction produces negative fixed background",
        )
    reconstructed = [background[index] + shift[index] for index in range(len(slots))]
    if reconstructed != [slot.measured_energy_wh for slot in slots]:
        _fail(
            "VERIFIER_DECOMPOSITION_FAILED",
            "Reference subtraction does not reconstruct the measured profile",
        )
    return background, references


def _recompute_bill(
    scenario: ScenarioInput,
    profile_energy: list[int],
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    dated_facts: DatedEligibilityFacts | None,
) -> ReplayResult:
    if scenario.tariff_version_id != bundle.ir.tariff_version_id:
        _fail(
            "VERIFIER_TARIFF_VERSION_MISMATCH",
            "Scenario and compiled tariff versions differ",
        )
    intervals = tuple(
        ReplayInterval(
            start_utc_ns=(
                timegm(slot.slot_start_utc.utctimetuple()) * 1_000_000_000
                + slot.slot_start_utc.microsecond * 1_000
            ),
            duration_seconds=slot.duration_seconds,
            energy_wh=profile_energy[index],
        )
        for index, slot in enumerate(scenario.profile_slots)
    )
    request = IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256=scenario.profile_content_sha256,
        account_facts=account_facts,
        energy_wh=sum(profile_energy),
        intervals=intervals,
        dated_eligibility_facts=dated_facts,
    )
    return replay_compiled_tariff(bundle, request)


def _objective(
    scenario: ScenarioInput,
    candidate_by_id: dict[UUID, tuple[int, ...]],
    references: dict[UUID, tuple[int, ...]],
    supported_cost_cents: int,
) -> ObjectiveTuple:
    changed = 0
    completion = 0
    stable_score = 0
    position = 0
    for _, occurrence in _canonical_occurrences(scenario):
        candidate = candidate_by_id[occurrence.occurrence_id]
        reference = references[occurrence.occurrence_id]
        changed += sum(left != right for left, right in zip(candidate, reference, strict=True))
        positive_slots = [index + 1 for index, amount in enumerate(candidate) if amount > 0]
        if not positive_slots:
            _fail(
                "VERIFIER_ENERGY_CONSERVATION_FAILED",
                "A positive-energy occurrence has no completion slot",
                occurrence_id=str(occurrence.occurrence_id),
            )
        completion += positive_slots[-1]
        for amount in candidate:
            position += 1
            stable_score += amount * position
    return ObjectiveTuple(
        supported_cost_cents=supported_cost_cents,
        changed_occurrence_slot_count=changed,
        completion_slot_index_sum=completion,
        stable_slot_order_score=stable_score,
    )


def candidate_from_reference(scenario: ScenarioInput) -> CandidateSchedule:
    """Build the canonical complete candidate vector from the admitted reference."""

    return CandidateSchedule(
        occurrences=tuple(
            OccurrenceSchedule(
                occurrence_id=occurrence.occurrence_id,
                slots=tuple(
                    ScheduleSlot(
                        slot_start_utc=slot.slot_start_utc,
                        duration_seconds=slot.duration_seconds,
                        energy_wh=slot.energy_wh,
                    )
                    for slot in occurrence.reference_schedule
                ),
            )
            for _, occurrence in _canonical_occurrences(scenario)
        )
    )


def verify_candidate_schedule(
    scenario: ScenarioInput,
    candidate: CandidateSchedule,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    *,
    dated_facts: DatedEligibilityFacts | None = None,
    claimed_supported_cost_cents: int | None = None,
) -> VerifiedSchedule:
    """Verify a candidate without importing production solver or lowering code."""

    slots = scenario.profile_slots
    boundaries = _profile_boundaries(slots)
    _verify_disjoint_windows(scenario)
    canonical_occurrences = _canonical_occurrences(scenario)
    expected_ids = {occurrence.occurrence_id for _, occurrence in canonical_occurrences}
    submitted = _schedule_map(candidate, expected_ids)
    background, references = _background_and_reference(scenario)
    candidate_by_id: dict[UUID, tuple[int, ...]] = {}
    flexible = [0] * len(slots)
    canonical_schedules: list[OccurrenceSchedule] = []
    for load, occurrence in canonical_occurrences:
        occurrence_schedule = submitted[occurrence.occurrence_id]
        energy = _energy_vector(occurrence_schedule, slots)
        start, end = _window_indices(occurrence, boundaries)
        _verify_execution(load, occurrence, energy, slots, start, end)
        candidate_by_id[occurrence.occurrence_id] = energy
        for index, amount in enumerate(energy):
            flexible[index] += amount
        canonical_schedules.append(occurrence_schedule)
    profile = [background[index] + flexible[index] for index in range(len(slots))]
    constraints = scenario.electrical_constraints
    for index, slot in enumerate(slots):
        if (
            constraints.site_import_cap_w is not None
            and profile[index] * 3_600 > constraints.site_import_cap_w * slot.duration_seconds
        ):
            _fail(
                "VERIFIER_SITE_IMPORT_CAP_VIOLATION",
                "Candidate profile exceeds the site average-power cap",
                slot_index=index,
            )
        if (
            constraints.flexible_load_aggregate_cap_w is not None
            and flexible[index] * 3_600
            > constraints.flexible_load_aggregate_cap_w * slot.duration_seconds
        ):
            _fail(
                "VERIFIER_FLEXIBLE_LOAD_CAP_VIOLATION",
                "Candidate flexible energy exceeds the aggregate average-power cap",
                slot_index=index,
            )
    billing = _recompute_bill(scenario, profile, bundle, account_facts, dated_facts)
    if (
        claimed_supported_cost_cents is not None
        and claimed_supported_cost_cents != billing.supported_calculated_cents
    ):
        _fail(
            "VERIFIER_COST_MISMATCH",
            "Claimed cost differs from fresh reference billing",
            claimed_supported_cost_cents=claimed_supported_cost_cents,
            recomputed_supported_cost_cents=billing.supported_calculated_cents,
        )
    objective = _objective(
        scenario,
        candidate_by_id,
        references,
        billing.supported_calculated_cents,
    )
    candidate_profile = tuple(
        CandidateProfileSlot(
            slot_start_utc=slot.slot_start_utc,
            duration_seconds=slot.duration_seconds,
            energy_wh=profile[index],
        )
        for index, slot in enumerate(slots)
    )
    checked_codes = (
        "VERIFIER_DECOMPOSITION_FAILED",
        "VERIFIER_ENERGY_CONSERVATION_FAILED",
        "VERIFIER_WINDOW_VIOLATION",
        "VERIFIER_MAXIMUM_POWER_VIOLATION",
        "VERIFIER_MINIMUM_POWER_VIOLATION",
        "VERIFIER_CONTIGUITY_VIOLATION",
        "VERIFIER_SITE_IMPORT_CAP_VIOLATION",
        "VERIFIER_FLEXIBLE_LOAD_CAP_VIOLATION",
        "VERIFIER_COST_MISMATCH",
    )
    canonical_candidate = CandidateSchedule(occurrences=tuple(canonical_schedules))
    payload = {
        "schedule": canonical_candidate.model_dump(mode="json"),
        "objective": objective.model_dump(mode="json"),
        "candidate_profile": [item.model_dump(mode="json") for item in candidate_profile],
        "billing_result_sha256": billing.result_sha256,
        "checked_constraint_codes": checked_codes,
    }
    record = VerificationRecord(
        objective=objective,
        candidate_profile=candidate_profile,
        billing_result_sha256=billing.result_sha256,
        checked_constraint_codes=checked_codes,
        verification_sha256=canonical_content_sha256(
            b"RateReplay.IndependentScheduleVerification.v1", payload
        ),
    )
    return VerifiedSchedule(
        schedule=canonical_candidate,
        record=record,
        billing_result=billing,
    )


def select_strict_improvement(
    incumbent: VerifiedSchedule | None,
    reference: VerifiedSchedule,
) -> SelectionDecision:
    """Select an incumbent only when its complete objective tuple is smaller."""

    if incumbent is None:
        return SelectionDecision(
            selected=reference,
            incumbent=None,
            reference=reference,
            selected_source="REFERENCE",
            reason="NO_VERIFIED_INCUMBENT",
        )
    if incumbent.record.objective.ordered_values() < reference.record.objective.ordered_values():
        return SelectionDecision(
            selected=incumbent,
            incumbent=incumbent,
            reference=reference,
            selected_source="SOLVER_INCUMBENT",
            reason="INCUMBENT_STRICTLY_BETTER",
        )
    return SelectionDecision(
        selected=reference,
        incumbent=incumbent,
        reference=reference,
        selected_source="REFERENCE",
        reason="REFERENCE_EQUAL_OR_BETTER",
    )
