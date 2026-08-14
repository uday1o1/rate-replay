"""Deterministic staged CP-SAT searches with verified reference selection."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
from typing import Literal

from ortools.sat.python import cp_model
from ratereplay_tariffs.compiled import CompilationBundle
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

from ratereplay_optimizer.lowering import LoweredScenarioModel
from ratereplay_optimizer.lowering import compile_scenario_model as compile_scenario_model
from ratereplay_optimizer.models import (
    CandidateSchedule,
    HeuristicStageRecord,
    LoweringRecord,
    OccurrenceSchedule,
    ScheduleSlot,
    SolverConfiguration,
    SolverStageRecord,
    ValidatedScenario,
)
from ratereplay_optimizer.verification import (
    ScheduleVerificationError,
    SelectionDecision,
    VerifiedSchedule,
    candidate_from_reference,
    select_strict_improvement,
    verify_candidate_schedule,
)

ExactSearchStatus = Literal[
    "OPTIMAL",
    "BEST_FOUND",
    "UNKNOWN",
    "MODEL_INVALID",
    "MODEL_CONTRACT_VIOLATION",
    "UNVERIFIED_INCUMBENT",
]
HeuristicSearchStatus = Literal[
    "HEURISTIC_PROXY_OPTIMAL",
    "HEURISTIC_BEST_FOUND",
    "HEURISTIC_NO_INCUMBENT",
    "HEURISTIC_MODEL_INVALID",
    "HEURISTIC_MODEL_CONTRACT_VIOLATION",
    "HEURISTIC_UNVERIFIED_INCUMBENT",
]


class OptimizationExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, **witness: object) -> None:
        super().__init__(message)
        self.code = code
        self.witness = witness


@dataclass(frozen=True, slots=True)
class ExactOptimizationResult:
    search_status: ExactSearchStatus
    selected: SelectionDecision
    stage_records: tuple[SolverStageRecord, ...]
    highest_objective_stage_proved_optimal: int
    first_open_stage: int | None
    best_supported_cost_bound: float | None
    absolute_cost_gap_cents: float | None
    relative_cost_gap: float | None
    solver_configuration: SolverConfiguration
    lowering_record: LoweringRecord
    result_sha256: str


@dataclass(frozen=True, slots=True)
class HeuristicOptimizationResult:
    search_status: HeuristicSearchStatus
    selection_outcome: Literal[
        "HEURISTIC_INCUMBENT_SELECTED",
        "HEURISTIC_REFERENCE_DOMINATES",
        "HEURISTIC_REFERENCE_FALLBACK",
    ]
    selected: VerifiedSchedule
    incumbent: VerifiedSchedule | None
    reference: VerifiedSchedule
    incumbent_proxy_pair: tuple[int, int] | None
    reference_proxy_pair: tuple[int, int]
    stage_records: tuple[HeuristicStageRecord, ...]
    solver_configuration: SolverConfiguration
    lowering_record: LoweringRecord
    fallback_reason: str | None
    result_sha256: str


def default_solver_configuration(
    *,
    max_deterministic_time_per_stage: float = 5.0,
) -> SolverConfiguration:
    return SolverConfiguration(
        solver_version=version("ortools"),
        max_deterministic_time_per_stage=max_deterministic_time_per_stage,
    )


def _configured_solver(configuration: SolverConfiguration) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = configuration.num_search_workers
    solver.parameters.random_seed = configuration.random_seed
    solver.parameters.max_deterministic_time = configuration.max_deterministic_time_per_stage
    solver.parameters.randomize_search = configuration.randomize_search
    solver.parameters.log_search_progress = configuration.log_search_progress
    return solver


def _extract_candidate(
    lowered: LoweredScenarioModel,
    validated: ValidatedScenario,
    solver: cp_model.CpSolver,
) -> CandidateSchedule:
    slots = validated.scenario.profile_slots
    return CandidateSchedule(
        occurrences=tuple(
            OccurrenceSchedule(
                occurrence_id=occurrence_id,
                slots=tuple(
                    ScheduleSlot(
                        slot_start_utc=slot.slot_start_utc,
                        duration_seconds=slot.duration_seconds,
                        energy_wh=solver.value(variable),
                    )
                    for slot, variable in zip(slots, variables, strict=True)
                ),
            )
            for occurrence_id, variables in lowered.energy_by_occurrence.items()
        )
    )


def _verify_solver_incumbent(
    lowered: LoweredScenarioModel,
    validated: ValidatedScenario,
    solver: cp_model.CpSolver,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    dated_facts: DatedEligibilityFacts | None,
) -> VerifiedSchedule:
    candidate = _extract_candidate(lowered, validated, solver)
    try:
        verified = verify_candidate_schedule(
            validated.scenario,
            candidate,
            bundle,
            account_facts,
            dated_facts=dated_facts,
            claimed_supported_cost_cents=solver.value(lowered.objectives.supported_cost),
        )
    except ScheduleVerificationError as error:
        raise OptimizationExecutionError(
            "UNVERIFIED_SOLVER_INCUMBENT",
            "The independent verifier rejected a solver incumbent",
            verifier_code=error.code,
            verifier_witness=error.witness,
        ) from error
    solver_values = (
        solver.value(lowered.objectives.supported_cost),
        solver.value(lowered.objectives.changed_occurrence_slots),
        solver.value(lowered.objectives.completion_slot_index_sum),
        solver.value(lowered.objectives.stable_slot_order_score),
    )
    if solver_values != verified.record.objective.ordered_values():
        raise OptimizationExecutionError(
            "SOLVER_OBJECTIVE_EXTRACTION_MISMATCH",
            "Solver objective values differ from independent recomputation",
            solver_values=solver_values,
            verifier_values=verified.record.objective.ordered_values(),
        )
    return verified


def _status_name(solver: cp_model.CpSolver, status: cp_model.CpSolverStatus) -> str:
    return solver.status_name(status)


def _objective_bound(solver: cp_model.CpSolver, status_name: str) -> float | None:
    if status_name in {"OPTIMAL", "FEASIBLE"}:
        return solver.best_objective_bound
    return None


def _incumbent_value(
    solver: cp_model.CpSolver,
    expression: cp_model.LinearExpr,
    status_name: str,
) -> int | None:
    if status_name in {"OPTIMAL", "FEASIBLE"}:
        return solver.value(expression)
    return None


def optimize_exact(
    validated: ValidatedScenario,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    *,
    dated_facts: DatedEligibilityFacts | None = None,
    configuration: SolverConfiguration | None = None,
) -> ExactOptimizationResult:
    """Run four lexicographic stages and verify every available incumbent."""

    resolved_configuration = configuration or default_solver_configuration()
    reference = verify_candidate_schedule(
        validated.scenario,
        candidate_from_reference(validated.scenario),
        bundle,
        account_facts,
        dated_facts=dated_facts,
    )
    lowered = compile_scenario_model(
        validated,
        bundle,
        account_facts,
        reference.billing_result,
    )
    stages = (
        ("SUPPORTED_COST", lowered.objectives.supported_cost),
        ("CHANGED_OCCURRENCE_SLOTS", lowered.objectives.changed_occurrence_slots),
        ("COMPLETION_SLOT_INDEX_SUM", lowered.objectives.completion_slot_index_sum),
        ("STABLE_SLOT_ORDER_SCORE", lowered.objectives.stable_slot_order_score),
    )
    records: list[SolverStageRecord] = []
    incumbent: VerifiedSchedule | None = None
    highest_optimal = 0
    first_open: int | None = None
    search_status: ExactSearchStatus = "UNKNOWN"
    primary_bound: float | None = None
    for stage_index, (stage_name, expression) in enumerate(stages, start=1):
        lowered.model.minimize(expression)
        solver = _configured_solver(resolved_configuration)
        status = solver.solve(lowered.model)
        status_name = _status_name(solver, status)
        incumbent_value = _incumbent_value(solver, expression, status_name)
        bound = _objective_bound(solver, status_name)
        if stage_index == 1:
            primary_bound = bound
        fixed_optimum = incumbent_value if status_name == "OPTIMAL" else None
        records.append(
            SolverStageRecord.model_validate(
                {
                    "stage_index": stage_index,
                    "stage_name": stage_name,
                    "status": status_name,
                    "incumbent_value": incumbent_value,
                    "best_objective_bound": bound,
                    "fixed_optimum": fixed_optimum,
                }
            )
        )
        if status_name in {"OPTIMAL", "FEASIBLE"}:
            incumbent = _verify_solver_incumbent(
                lowered,
                validated,
                solver,
                bundle,
                account_facts,
                dated_facts,
            )
        if status_name == "OPTIMAL":
            highest_optimal = stage_index
            lowered.model.add(expression == cast_int(fixed_optimum))
            if stage_index == len(stages):
                search_status = "OPTIMAL"
            continue
        first_open = stage_index
        if status_name == "FEASIBLE":
            search_status = "BEST_FOUND"
        elif status_name == "UNKNOWN":
            search_status = "BEST_FOUND" if incumbent is not None else "UNKNOWN"
        elif status_name == "MODEL_INVALID":
            search_status = "MODEL_INVALID"
        elif status_name == "INFEASIBLE":
            search_status = "MODEL_CONTRACT_VIOLATION"
        else:
            raise OptimizationExecutionError(
                "UNRECOGNIZED_SOLVER_STATUS",
                "CP-SAT returned an unrecognized status",
                status=status_name,
            )
        break
    selection = select_strict_improvement(incumbent, reference)
    incumbent_cost = (
        incumbent.record.objective.supported_cost_cents if incumbent is not None else None
    )
    absolute_gap: float | None = None
    relative_gap: float | None = None
    if incumbent_cost is not None and primary_bound is not None:
        absolute_gap = max(0.0, incumbent_cost - primary_bound)
        relative_gap = absolute_gap / max(1, abs(incumbent_cost))
    result_payload = {
        "search_status": search_status,
        "selected_source": selection.selected_source,
        "selection_reason": selection.reason,
        "selected_verification_sha256": selection.selected.record.verification_sha256,
        "incumbent_verification_sha256": (
            incumbent.record.verification_sha256 if incumbent is not None else None
        ),
        "reference_verification_sha256": reference.record.verification_sha256,
        "stage_records": [record.model_dump(mode="json") for record in records],
        "highest_objective_stage_proved_optimal": highest_optimal,
        "first_open_stage": first_open,
        "best_supported_cost_bound": primary_bound,
        "absolute_cost_gap_cents": absolute_gap,
        "relative_cost_gap": relative_gap,
        "solver_configuration": resolved_configuration.model_dump(mode="json"),
        "lowering_sha256": lowered.record.lowering_sha256,
    }
    return ExactOptimizationResult(
        search_status=search_status,
        selected=selection,
        stage_records=tuple(records),
        highest_objective_stage_proved_optimal=highest_optimal,
        first_open_stage=first_open,
        best_supported_cost_bound=primary_bound,
        absolute_cost_gap_cents=absolute_gap,
        relative_cost_gap=relative_gap,
        solver_configuration=resolved_configuration,
        lowering_record=lowered.record,
        result_sha256=canonical_content_sha256(
            b"RateReplay.ExactOptimizationResult.v1", result_payload
        ),
    )


def cast_int(value: int | None) -> int:
    if value is None:
        raise OptimizationExecutionError(
            "MISSING_PROVED_OPTIMUM",
            "An optimal stage did not expose its integer optimum",
        )
    return value


def _proxy_pair(
    verified: VerifiedSchedule,
    off_peak_ranks: tuple[int, ...],
) -> tuple[int, int]:
    proxy = sum(
        slot.energy_wh * off_peak_ranks[index]
        for occurrence in verified.schedule.occurrences
        for index, slot in enumerate(occurrence.slots)
    )
    return proxy, verified.record.objective.stable_slot_order_score


def optimize_off_peak_heuristic(
    validated: ValidatedScenario,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    *,
    dated_facts: DatedEligibilityFacts | None = None,
    configuration: SolverConfiguration | None = None,
) -> HeuristicOptimizationResult:
    """Run the two-stage off-peak proxy without claiming bill optimality."""

    resolved_configuration = configuration or default_solver_configuration()
    reference = verify_candidate_schedule(
        validated.scenario,
        candidate_from_reference(validated.scenario),
        bundle,
        account_facts,
        dated_facts=dated_facts,
    )
    lowered = compile_scenario_model(
        validated,
        bundle,
        account_facts,
        reference.billing_result,
    )
    stages = (
        ("OFF_PEAK_PROXY_RANK", lowered.objectives.proxy_rank_score),
        ("STABLE_SLOT_ORDER_SCORE", lowered.objectives.stable_slot_order_score),
    )
    records: list[HeuristicStageRecord] = []
    incumbent: VerifiedSchedule | None = None
    fallback_reason: str | None = None
    search_status: HeuristicSearchStatus = "HEURISTIC_NO_INCUMBENT"
    for stage_index, (stage_name, expression) in enumerate(stages, start=1):
        lowered.model.minimize(expression)
        solver = _configured_solver(resolved_configuration)
        status = solver.solve(lowered.model)
        status_name = _status_name(solver, status)
        incumbent_value = _incumbent_value(solver, expression, status_name)
        bound = _objective_bound(solver, status_name)
        fixed_optimum = incumbent_value if status_name == "OPTIMAL" else None
        records.append(
            HeuristicStageRecord.model_validate(
                {
                    "stage_index": stage_index,
                    "stage_name": stage_name,
                    "status": status_name,
                    "incumbent_value": incumbent_value,
                    "best_objective_bound": bound,
                    "fixed_optimum": fixed_optimum,
                }
            )
        )
        if status_name in {"OPTIMAL", "FEASIBLE"}:
            incumbent = _verify_solver_incumbent(
                lowered,
                validated,
                solver,
                bundle,
                account_facts,
                dated_facts,
            )
        if status_name == "OPTIMAL":
            lowered.model.add(expression == cast_int(fixed_optimum))
            if stage_index == len(stages):
                search_status = "HEURISTIC_PROXY_OPTIMAL"
            continue
        if status_name in {"FEASIBLE", "UNKNOWN"} and incumbent is not None:
            search_status = "HEURISTIC_BEST_FOUND"
            fallback_reason = f"{stage_name}_{status_name}"
        elif status_name == "MODEL_INVALID":
            search_status = "HEURISTIC_MODEL_INVALID"
            fallback_reason = f"{stage_name}_MODEL_INVALID"
        elif status_name == "INFEASIBLE":
            search_status = "HEURISTIC_MODEL_CONTRACT_VIOLATION"
            fallback_reason = f"{stage_name}_INFEASIBLE"
        else:
            search_status = "HEURISTIC_NO_INCUMBENT"
            fallback_reason = f"{stage_name}_{status_name}"
        break
    reference_pair = _proxy_pair(reference, lowered.record.off_peak_ranks)
    incumbent_pair = (
        _proxy_pair(incumbent, lowered.record.off_peak_ranks) if incumbent is not None else None
    )
    selection_outcome: Literal[
        "HEURISTIC_INCUMBENT_SELECTED",
        "HEURISTIC_REFERENCE_DOMINATES",
        "HEURISTIC_REFERENCE_FALLBACK",
    ]
    if incumbent is None:
        selected = reference
        selection_outcome = "HEURISTIC_REFERENCE_FALLBACK"
    elif incumbent_pair is not None and incumbent_pair < reference_pair:
        selected = incumbent
        selection_outcome = "HEURISTIC_INCUMBENT_SELECTED"
    else:
        selected = reference
        selection_outcome = "HEURISTIC_REFERENCE_DOMINATES"
    result_payload = {
        "heuristic_contract_version": "off-peak-heuristic-v1",
        "search_status": search_status,
        "selection_outcome": selection_outcome,
        "selected_verification_sha256": selected.record.verification_sha256,
        "incumbent_proxy_pair": incumbent_pair,
        "reference_proxy_pair": reference_pair,
        "stage_records": [record.model_dump(mode="json") for record in records],
        "solver_configuration": resolved_configuration.model_dump(mode="json"),
        "rank_calendar_sha256": lowered.record.rank_calendar_sha256,
        "lowering_sha256": lowered.record.lowering_sha256,
        "fallback_reason": fallback_reason,
    }
    return HeuristicOptimizationResult(
        search_status=search_status,
        selection_outcome=selection_outcome,
        selected=selected,
        incumbent=incumbent,
        reference=reference,
        incumbent_proxy_pair=incumbent_pair,
        reference_proxy_pair=reference_pair,
        stage_records=tuple(records),
        solver_configuration=resolved_configuration,
        lowering_record=lowered.record,
        fallback_reason=fallback_reason,
        result_sha256=canonical_content_sha256(
            b"RateReplay.OffPeakHeuristicResult.v1", result_payload
        ),
    )
