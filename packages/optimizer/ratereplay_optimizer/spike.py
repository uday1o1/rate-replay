"""Exact CP-SAT lowering used to prove the optimizer boundary."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ortools.sat.python import cp_model

SIGNED_INT64_MAX = 2**63 - 1


@dataclass(frozen=True, order=True, slots=True)
class ObjectiveTuple:
    supported_cost_score: int
    changed_entries: int
    completion_index_sum: int
    stable_slot_order_score: int


@dataclass(frozen=True, slots=True)
class SpikeResult:
    status: str
    schedule_wh: tuple[int, ...]
    objective: ObjectiveTuple


def objective_tuple(
    schedule_wh: tuple[int, ...],
    *,
    reference_wh: tuple[int, ...],
    rate_microdollars_per_kwh: tuple[int, ...],
) -> ObjectiveTuple:
    if not (len(schedule_wh) == len(reference_wh) == len(rate_microdollars_per_kwh)):
        raise ValueError("Schedule vectors must have identical lengths")
    positive = [index for index, energy in enumerate(schedule_wh, start=1) if energy > 0]
    return ObjectiveTuple(
        supported_cost_score=sum(
            energy * rate
            for energy, rate in zip(schedule_wh, rate_microdollars_per_kwh, strict=True)
        ),
        changed_entries=sum(
            candidate != reference
            for candidate, reference in zip(schedule_wh, reference_wh, strict=True)
        ),
        completion_index_sum=max(positive, default=0),
        stable_slot_order_score=sum(
            index * energy for index, energy in enumerate(schedule_wh, start=1)
        ),
    )


def _solve_stage(
    model: cp_model.CpModel,
    expression: cp_model.LinearExpr | cp_model.IntVar,
) -> tuple[cp_model.CpSolver, int]:
    model.minimize(expression)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 20_260_813
    solver.parameters.max_deterministic_time = 1.0
    status = solver.solve(model)
    if status != cp_model.OPTIMAL:
        raise RuntimeError(f"SPIKE_SOLVER_NOT_OPTIMAL:{solver.status_name(status)}")
    optimum = int(solver.value(expression))
    model.add(expression == optimum)
    return solver, optimum


def solve_interruptible_spike(
    *,
    reference_wh: tuple[int, ...],
    rate_microdollars_per_kwh: tuple[int, ...],
    required_energy_wh: int,
    maximum_slot_energy_wh: int,
) -> SpikeResult:
    """Solve all four objective stages with exact signed 64-bit coefficients."""

    slot_count = len(reference_wh)
    if slot_count == 0 or slot_count != len(rate_microdollars_per_kwh):
        raise ValueError("Spike requires nonempty equal-length vectors")
    max_cost = maximum_slot_energy_wh * sum(rate_microdollars_per_kwh)
    max_stable = maximum_slot_energy_wh * slot_count * (slot_count + 1) // 2
    if max(max_cost, max_stable) > SIGNED_INT64_MAX:
        raise OverflowError("INT64_OBJECTIVE_BOUND")
    model = cp_model.CpModel()
    energy = [
        model.new_int_var(0, maximum_slot_energy_wh, f"energy_{index}")
        for index in range(slot_count)
    ]
    model.add(sum(energy) == required_energy_wh)
    changed: list[cp_model.IntVar] = []
    active: list[cp_model.IntVar] = []
    completion_terms: list[cp_model.IntVar] = []
    for index, variable in enumerate(energy):
        is_changed = model.new_bool_var(f"changed_{index}")
        model.add(variable == reference_wh[index]).only_enforce_if(is_changed.Not())
        model.add(variable != reference_wh[index]).only_enforce_if(is_changed)
        changed.append(is_changed)
        is_active = model.new_bool_var(f"active_{index}")
        model.add(variable >= 1).only_enforce_if(is_active)
        model.add(variable == 0).only_enforce_if(is_active.Not())
        active.append(is_active)
        term = model.new_int_var(0, index + 1, f"completion_term_{index}")
        model.add(term == index + 1).only_enforce_if(is_active)
        model.add(term == 0).only_enforce_if(is_active.Not())
        completion_terms.append(term)
    completion = model.new_int_var(0, slot_count, "completion")
    model.add_max_equality(completion, completion_terms)
    cost = cp_model.LinearExpr.weighted_sum(energy, rate_microdollars_per_kwh)
    changed_count = cp_model.LinearExpr.sum(changed)
    stable = cp_model.LinearExpr.weighted_sum(energy, range(1, slot_count + 1))
    _, cost_optimum = _solve_stage(model, cost)
    _, changed_optimum = _solve_stage(model, changed_count)
    _, completion_optimum = _solve_stage(model, completion)
    solver, stable_optimum = _solve_stage(model, stable)
    schedule = tuple(solver.value(variable) for variable in energy)
    objective = objective_tuple(
        schedule,
        reference_wh=reference_wh,
        rate_microdollars_per_kwh=rate_microdollars_per_kwh,
    )
    expected_objective = ObjectiveTuple(
        cost_optimum, changed_optimum, completion_optimum, stable_optimum
    )
    if objective != expected_objective:
        raise RuntimeError("SPIKE_OBJECTIVE_EXTRACTION_MISMATCH")
    return SpikeResult(status="OPTIMAL", schedule_wh=schedule, objective=objective)


def exhaustive_spike_oracle(
    *,
    reference_wh: tuple[int, ...],
    rate_microdollars_per_kwh: tuple[int, ...],
    required_energy_wh: int,
    maximum_slot_energy_wh: int,
) -> tuple[ObjectiveTuple, frozenset[tuple[int, ...]]]:
    """Enumerate independently without importing production constraint construction."""

    def bounded_compositions(remaining: int, slots: int) -> Iterator[tuple[int, ...]]:
        if slots == 0:
            if remaining == 0:
                yield ()
            return
        lower = max(0, remaining - maximum_slot_energy_wh * (slots - 1))
        upper = min(maximum_slot_energy_wh, remaining)
        for first in range(lower, upper + 1):
            for suffix in bounded_compositions(remaining - first, slots - 1):
                yield (first, *suffix)

    candidates = bounded_compositions(required_energy_wh, len(reference_wh))
    scored = [
        (
            objective_tuple(
                candidate,
                reference_wh=reference_wh,
                rate_microdollars_per_kwh=rate_microdollars_per_kwh,
            ),
            candidate,
        )
        for candidate in candidates
    ]
    if not scored:
        raise ValueError("Spike instance is infeasible")
    optimum = min(score for score, _ in scored)
    return optimum, frozenset(candidate for score, candidate in scored if score == optimum)
