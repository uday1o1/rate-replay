"""Strict flexible-load and historical-scenario contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    """Reject coercion and unknown fields, and freeze validated contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _is_utc(value: datetime) -> bool:
    offset = value.utcoffset()
    return offset is not None and offset.total_seconds() == 0


class CanonicalProfileSlot(FrozenModel):
    slot_start_utc: datetime
    duration_seconds: int = Field(gt=0)
    measured_energy_wh: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_utc(self) -> CanonicalProfileSlot:
        if not _is_utc(self.slot_start_utc):
            raise ValueError("profile slot start must be a UTC instant")
        return self


class ReferenceSlot(FrozenModel):
    slot_start_utc: datetime
    duration_seconds: int = Field(gt=0)
    energy_wh: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_utc(self) -> ReferenceSlot:
        if not _is_utc(self.slot_start_utc):
            raise ValueError("reference slot start must be a UTC instant")
        return self


class InterruptibleModulatingSpec(FrozenModel):
    execution_type: Literal["INTERRUPTIBLE_MODULATING"]
    maximum_power_w: int = Field(gt=0)
    minimum_power_when_active_w: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_power_range(self) -> InterruptibleModulatingSpec:
        if self.minimum_power_when_active_w > self.maximum_power_w:
            raise ValueError("minimum active power cannot exceed maximum power")
        return self


class ContiguousFixedShapeSpec(FrozenModel):
    execution_type: Literal["CONTIGUOUS_FIXED_SHAPE"]
    fixed_slot_shape_wh: tuple[Annotated[int, Field(ge=0)], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_positive_shape(self) -> ContiguousFixedShapeSpec:
        if sum(self.fixed_slot_shape_wh) <= 0:
            raise ValueError("fixed shape must contain positive energy")
        return self


ExecutionSpec = Annotated[
    InterruptibleModulatingSpec | ContiguousFixedShapeSpec,
    Field(discriminator="execution_type"),
]


class LoadOccurrence(FrozenModel):
    occurrence_id: UUID
    required_energy_wh: int = Field(gt=0)
    earliest_start_utc: datetime
    deadline_utc: datetime
    reference_schedule: tuple[ReferenceSlot, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_window(self) -> LoadOccurrence:
        for value in (self.earliest_start_utc, self.deadline_utc):
            if not _is_utc(value):
                raise ValueError("occurrence endpoints must be UTC instants")
        if self.deadline_utc <= self.earliest_start_utc:
            raise ValueError("occurrence window must be nonempty and half-open")
        return self


class FlexibleLoad(FrozenModel):
    load_id: UUID
    physical_asset_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    kind: Literal["EV", "DISHWASHER", "WASHER", "DRYER", "POOL_PUMP", "CUSTOM"]
    mode: Literal["SHIFT_EXISTING", "HISTORICAL_ADDITION"]
    execution_spec: ExecutionSpec
    occurrences: tuple[LoadOccurrence, ...] = Field(min_length=1)


class ScenarioElectricalConstraints(FrozenModel):
    site_import_cap_w: int | None = Field(default=None, gt=0)
    flexible_load_aggregate_cap_w: int | None = Field(default=None, gt=0)
    energy_basis: Literal["METER_SIDE"] = "METER_SIDE"


class ScenarioInput(FrozenModel):
    scenario_version: Literal["historical-flex-scenario-v1"]
    profile_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tariff_version_id: str = Field(min_length=1)
    profile_slots: tuple[CanonicalProfileSlot, ...] = Field(min_length=1)
    loads: tuple[FlexibleLoad, ...] = Field(min_length=1)
    electrical_constraints: ScenarioElectricalConstraints = ScenarioElectricalConstraints()


class EnergySlot(FrozenModel):
    slot_start_utc: datetime
    duration_seconds: int = Field(gt=0)
    energy_wh: int = Field(ge=0)


class ScenarioDecomposition(FrozenModel):
    decomposition_version: Literal["scenario-decomposition-v1"] = "scenario-decomposition-v1"
    calculation_time_mode: Literal["HISTORICAL_REPLAY"] = "HISTORICAL_REPLAY"
    historical_addition_label: Literal["HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"] = (
        "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"
    )
    fixed_background: tuple[EnergySlot, ...]
    shift_existing_reference: tuple[EnergySlot, ...]
    historical_addition_reference: tuple[EnergySlot, ...]
    reconstructed_measured_profile: tuple[EnergySlot, ...]
    unchanged_reference_profile: tuple[EnergySlot, ...]
    exact_measured_reconstruction: Literal[True]


class ReferenceValidationRecord(FrozenModel):
    validation_version: Literal["reference-validation-v1"] = "reference-validation-v1"
    status: Literal["VALID"] = "VALID"
    load_count: int = Field(gt=0)
    occurrence_count: int = Field(gt=0)
    slot_count: int = Field(gt=0)
    checked_constraint_codes: tuple[str, ...]


class ValidatedScenario(FrozenModel):
    scenario: ScenarioInput
    decomposition: ScenarioDecomposition
    reference_validation: ReferenceValidationRecord


class ScheduleSlot(FrozenModel):
    slot_start_utc: datetime
    duration_seconds: int = Field(gt=0)
    energy_wh: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_utc(self) -> ScheduleSlot:
        if not _is_utc(self.slot_start_utc):
            raise ValueError("schedule slot start must be a UTC instant")
        return self


class OccurrenceSchedule(FrozenModel):
    occurrence_id: UUID
    slots: tuple[ScheduleSlot, ...] = Field(min_length=1)


class CandidateSchedule(FrozenModel):
    schedule_version: Literal["candidate-schedule-v1"] = "candidate-schedule-v1"
    occurrences: tuple[OccurrenceSchedule, ...] = Field(min_length=1)


class ObjectiveTuple(FrozenModel):
    supported_cost_cents: int
    changed_occurrence_slot_count: int = Field(ge=0)
    completion_slot_index_sum: int = Field(ge=0)
    stable_slot_order_score: int = Field(ge=0)

    def ordered_values(self) -> tuple[int, int, int, int]:
        return (
            self.supported_cost_cents,
            self.changed_occurrence_slot_count,
            self.completion_slot_index_sum,
            self.stable_slot_order_score,
        )


class CandidateProfileSlot(FrozenModel):
    slot_start_utc: datetime
    duration_seconds: int = Field(gt=0)
    energy_wh: int = Field(ge=0)


class VerificationRecord(FrozenModel):
    verification_version: Literal["independent-schedule-verifier-v1"] = (
        "independent-schedule-verifier-v1"
    )
    status: Literal["VALID"] = "VALID"
    objective: ObjectiveTuple
    candidate_profile: tuple[CandidateProfileSlot, ...]
    billing_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_constraint_codes: tuple[str, ...]
    verification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OmittedChargeProof(FrozenModel):
    rule_id: str
    operator: str
    subterm: str
    algebraic_reason: Literal[
        "ACCOUNT_APPLICABILITY_FALSE",
        "NON_MONETARY_CLASSIFIER",
        "TOTAL_PROFILE_ENERGY_INVARIANT",
        "TOTAL_PROFILE_ENERGY_AND_BASELINE_INVARIANT",
        "BILLING_DAYS_INVARIANT",
        "BILLING_PERIOD_INVARIANT",
        "SUPPORTED_COST_ALWAYS_ZERO",
    ]
    invariant_total_energy_wh: int = Field(ge=0)
    billing_period_confined: Literal[True] = True


class ObjectiveBounds(FrozenModel):
    minimum_supported_cost_cents: int
    maximum_supported_cost_cents: int
    maximum_changed_occurrence_slot_count: int = Field(ge=0)
    maximum_completion_slot_index_sum: int = Field(ge=0)
    maximum_stable_slot_order_score: int = Field(ge=0)
    maximum_proxy_rank_score: int = Field(ge=0)


class LoweringRecord(FrozenModel):
    lowering_version: Literal["cp-sat-charge-lowering-v1"] = "cp-sat-charge-lowering-v1"
    tariff_version_id: str
    supported_operators: tuple[str, ...]
    constant_supported_cost_cents: int
    invariant_total_profile_energy_wh: int = Field(ge=0)
    omitted_charge_proofs: tuple[OmittedChargeProof, ...]
    off_peak_ranks: tuple[int, ...]
    rank_calendar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objective_bounds: ObjectiveBounds
    lowering_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SolverConfiguration(FrozenModel):
    configuration_version: Literal["deterministic-cp-sat-v1"] = "deterministic-cp-sat-v1"
    solver_name: Literal["OR-Tools CP-SAT"] = "OR-Tools CP-SAT"
    solver_version: str
    num_search_workers: Literal[1] = 1
    random_seed: int = 20_260_813
    max_deterministic_time_per_stage: float = Field(gt=0)
    randomize_search: Literal[False] = False
    log_search_progress: Literal[False] = False


class SolverStageRecord(FrozenModel):
    stage_index: int = Field(ge=1, le=4)
    stage_name: Literal[
        "SUPPORTED_COST",
        "CHANGED_OCCURRENCE_SLOTS",
        "COMPLETION_SLOT_INDEX_SUM",
        "STABLE_SLOT_ORDER_SCORE",
    ]
    status: Literal["OPTIMAL", "FEASIBLE", "UNKNOWN", "MODEL_INVALID", "INFEASIBLE"]
    incumbent_value: int | None
    best_objective_bound: float | None
    fixed_optimum: int | None


class HeuristicStageRecord(FrozenModel):
    stage_index: int = Field(ge=1, le=2)
    stage_name: Literal["OFF_PEAK_PROXY_RANK", "STABLE_SLOT_ORDER_SCORE"]
    status: Literal["OPTIMAL", "FEASIBLE", "UNKNOWN", "MODEL_INVALID", "INFEASIBLE"]
    incumbent_value: int | None
    best_objective_bound: float | None
    fixed_optimum: int | None
