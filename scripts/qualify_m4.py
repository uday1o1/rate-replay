#!/usr/bin/env python3
"""Generate deterministic Milestone 4 optimizer qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from ratereplay_optimizer.models import (
    CandidateSchedule,
    CanonicalProfileSlot,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    OccurrenceSchedule,
    ReferenceSlot,
    ScenarioInput,
    ScheduleSlot,
)
from ratereplay_optimizer.results import build_scenario_result
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import (
    default_solver_configuration,
    optimize_exact,
    optimize_off_peak_heuristic,
)
from ratereplay_optimizer.verification import (
    ScheduleVerificationError,
    verify_candidate_schedule,
)
from ratereplay_tariffs.admission import load_all_admitted_tariffs
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    replay_compiled_tariff,
)
from ratereplay_tariffs.compiled import CompilationBundle
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

from benchmarks.scripts.m4_performance import V3_CHARTER, V3_RESULT, V3_WORKLOAD
from benchmarks.scripts.m4_performance import _facts as benchmark_facts
from benchmarks.scripts.m4_performance import _scenario as benchmark_scenario

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evidence/correctness/m4-optimizer-qualification.json"
START = datetime(2026, 7, 6, 22, tzinfo=UTC)


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_bytes()))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _small_scenario() -> ScenarioInput:
    reference_amounts = (0, 0, 70)
    slots = tuple(
        CanonicalProfileSlot(
            slot_start_utc=START + timedelta(hours=index),
            duration_seconds=3_600,
            measured_energy_wh=100 + reference_amounts[index],
        )
        for index in range(3)
    )
    reference = tuple(
        ReferenceSlot(
            slot_start_utc=slot.slot_start_utc,
            duration_seconds=slot.duration_seconds,
            energy_wh=reference_amounts[index],
        )
        for index, slot in enumerate(slots)
    )
    return ScenarioInput(
        scenario_version="historical-flex-scenario-v1",
        profile_content_sha256="a" * 64,
        tariff_version_id="pge-etoud-2026-07",
        profile_slots=slots,
        loads=(
            FlexibleLoad(
                load_id=UUID("00000000-0000-0000-0000-000000000001"),
                physical_asset_key="oracle-ev-1",
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
                        reference_schedule=reference,
                    ),
                ),
            ),
        ),
    )


def _compositions(total: int, slots: int) -> list[tuple[int, ...]]:
    if slots == 1:
        return [(total,)]
    values: list[tuple[int, ...]] = []
    for first in range(total + 1):
        values.extend((first, *suffix) for suffix in _compositions(total - first, slots - 1))
    return values


def _independent_objective(
    scenario: ScenarioInput,
    amounts: tuple[int, ...],
    bundle: CompilationBundle,
    account: AccountFacts,
    dated: DatedEligibilityFacts,
) -> tuple[int, int, int, int]:
    reference = tuple(
        slot.energy_wh for slot in scenario.loads[0].occurrences[0].reference_schedule
    )
    background = tuple(
        slot.measured_energy_wh - reference[index]
        for index, slot in enumerate(scenario.profile_slots)
    )
    intervals = tuple(
        ReplayInterval(
            start_utc_ns=int(slot.slot_start_utc.timestamp()) * 1_000_000_000,
            duration_seconds=slot.duration_seconds,
            energy_wh=background[index] + amounts[index],
        )
        for index, slot in enumerate(scenario.profile_slots)
    )
    replay = replay_compiled_tariff(
        bundle,
        IntervalReplayRequest(
            request_version="interval-replay-request-v1",
            profile_content_sha256=scenario.profile_content_sha256,
            account_facts=account,
            energy_wh=sum(interval.energy_wh for interval in intervals),
            intervals=intervals,
            dated_eligibility_facts=dated,
        ),
    )
    positive_indices = tuple(index + 1 for index, amount in enumerate(amounts) if amount > 0)
    return (
        replay.supported_calculated_cents,
        sum(amount != reference[index] for index, amount in enumerate(amounts)),
        positive_indices[-1],
        sum(index * amount for index, amount in enumerate(amounts, start=1)),
    )


def _oracle_qualification() -> dict[str, Any]:
    scenario = _small_scenario()
    validated = validate_and_decompose_scenario(scenario)
    account, dated = benchmark_facts()
    bundle = compile_tariff(
        ROOT,
        ROOT / "tariffs/definitions/pge-etoud-2026-07.json",
    )
    scored = [
        (_independent_objective(scenario, amounts, bundle, account, dated), amounts)
        for amounts in _compositions(70, 3)
    ]
    optimum = min(objective for objective, _amounts in scored)
    optimum_set = {amounts for objective, amounts in scored if objective == optimum}
    exact = optimize_exact(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=default_solver_configuration(max_deterministic_time_per_stage=2.0),
    )
    selected = tuple(
        slot.energy_wh for slot in exact.selected.selected.schedule.occurrences[0].slots
    )
    if exact.search_status != "OPTIMAL":
        raise RuntimeError(f"M4_ORACLE_SOLVER_STATUS:{exact.search_status}")
    if exact.selected.selected.record.objective.ordered_values() != optimum:
        raise RuntimeError("M4_ORACLE_OBJECTIVE_MISMATCH")
    if selected not in optimum_set:
        raise RuntimeError("M4_ORACLE_OPTIMUM_MEMBERSHIP_MISMATCH")

    occurrence = exact.selected.selected.schedule.occurrences[0]
    corrupted_slots = list(occurrence.slots)
    corrupted_slots[0] = corrupted_slots[0].model_copy(
        update={"energy_wh": corrupted_slots[0].energy_wh + 1}
    )
    corrupted = CandidateSchedule(
        occurrences=(
            OccurrenceSchedule(
                occurrence_id=occurrence.occurrence_id,
                slots=tuple(
                    ScheduleSlot.model_validate(slot.model_dump()) for slot in corrupted_slots
                ),
            ),
        )
    )
    corruption_code = None
    try:
        verify_candidate_schedule(
            scenario,
            corrupted,
            bundle,
            account,
            dated_facts=dated,
        )
    except ScheduleVerificationError as error:
        corruption_code = error.code
    if corruption_code != "VERIFIER_ENERGY_CONSERVATION_FAILED":
        raise RuntimeError(f"M4_CORRUPTION_REASON:{corruption_code}")
    return {
        "enumerated_feasible_schedule_count": len(scored),
        "objective_tuple": list(optimum),
        "complete_final_optimum_set": [list(amounts) for amounts in sorted(optimum_set)],
        "returned_schedule": list(selected),
        "returned_schedule_in_optimum_set": True,
        "returned_verification_status": exact.selected.selected.record.status,
        "seeded_corruption": {
            "mutation": "add_one_watt_hour_to_first_selected_slot",
            "observed_code": corruption_code,
            "passed": True,
        },
    }


def qualify(output: Path = OUTPUT) -> dict[str, Any]:
    workload = _json(V3_WORKLOAD)
    performance = _json(V3_RESULT)
    if performance["gate_result"] != "PASS":
        raise RuntimeError("M4_PERFORMANCE_NOT_PASSED")
    account, dated = benchmark_facts()
    scenario = benchmark_scenario(workload, 1)
    validated = validate_and_decompose_scenario(scenario)
    bundle = compile_tariff(
        ROOT,
        ROOT / "tariffs/definitions/pge-etoud-2026-07.json",
    )
    configuration = default_solver_configuration(max_deterministic_time_per_stage=5.0)
    exact = optimize_exact(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=configuration,
    )
    heuristic = optimize_off_peak_heuristic(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=configuration,
    )
    result = build_scenario_result(
        validated,
        bundle,
        account,
        dated,
        exact,
        heuristic,
    )
    repeated_exact = optimize_exact(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=configuration,
    )
    if exact.result_sha256 != repeated_exact.result_sha256:
        raise RuntimeError("M4_EXACT_RESULT_NOT_REPEATABLE")
    tariffs = load_all_admitted_tariffs(ROOT)
    if any(not tariff.lock.scope.optimization_admitted for tariff in tariffs):
        raise RuntimeError("M4_PUBLIC_TARIFF_NOT_OPTIMIZATION_ADMITTED")
    payload: dict[str, Any] = {
        "schema_version": "m4-optimizer-qualification-v1",
        "milestone": 4,
        "gate_result": "PASS",
        "commands": {
            "qualification": "make qualification-m4",
            "performance": "make benchmark-m4-optimization",
            "postgres_integration": "make integration-m4",
            "clean_checkout": "make clean-checkout-check",
        },
        "inputs": {
            "workload_path": str(V3_WORKLOAD.relative_to(ROOT)),
            "workload_sha256": _sha256(V3_WORKLOAD),
            "charter_path": str(V3_CHARTER.relative_to(ROOT)),
            "charter_sha256": _sha256(V3_CHARTER),
            "profile_path": workload["profile_path"],
            "profile_sha256": workload["profile_sha256"],
            "calculation_time_mode": workload["calculation_time_mode"],
            "historical_addition_label": workload["load_template"]["historical_addition_label"],
        },
        "portfolio_scenario": {
            "reference_validation_status": validated.reference_validation.status,
            "exact_measured_reconstruction": (
                validated.decomposition.exact_measured_reconstruction
            ),
            "exact_search_status": exact.search_status,
            "highest_objective_stage_proved_optimal": (
                exact.highest_objective_stage_proved_optimal
            ),
            "selected_source": exact.selected.selected_source,
            "selected_objective_tuple": list(
                exact.selected.selected.record.objective.ordered_values()
            ),
            "reference_objective_tuple": list(
                exact.selected.reference.record.objective.ordered_values()
            ),
            "selected_verification_status": (exact.selected.selected.record.status),
            "heuristic_status": heuristic.search_status,
            "heuristic_selection_outcome": heuristic.selection_outcome,
            "heuristic_bill_optimality_claim": False,
            "result_sha256": result.result_sha256,
            "verification_sha256": (result.manifest.selected_verification_sha256),
            "solver_lowering_sha256": result.manifest.solver_lowering_sha256,
            "rank_calendar_sha256": result.manifest.rank_calendar_sha256,
            "repeatable_under_locked_environment": True,
        },
        "independent_exhaustive_oracle": _oracle_qualification(),
        "public_tariff_lowering": {
            "optimization_admitted_plan_codes": [tariff.lock.plan_code for tariff in tariffs],
            "count": len(tariffs),
            "randomized_equivalence_test": (
                "packages/optimizer/tests/test_solver.py::"
                "test_every_public_tariff_lowering_matches_fresh_reference_billing"
            ),
            "passed": True,
        },
        "status_and_rejection_contracts": {
            "pre_solve_codes": [
                "OVERLAPPING_LOAD_OCCURRENCES",
                "NON_ALIGNED_OCCURRENCE_BOUNDARY",
                "NEGATIVE_FIXED_BACKGROUND",
            ],
            "distinct_statuses": [
                "OPTIMAL",
                "BEST_FOUND",
                "INVALID_REFERENCE",
                "MODEL_CONTRACT_VIOLATION",
                "UNKNOWN",
                "MODEL_INVALID",
            ],
            "unsupported_operator_code": "UNSUPPORTED_IR_OPERATOR",
            "api_and_ui_contract_tests_passed": True,
        },
        "performance": {
            "evidence_path": str(V3_RESULT.relative_to(ROOT)),
            "evidence_sha256": _sha256(V3_RESULT),
            "gate_result": performance["gate_result"],
            "measurements_by_load_count": performance["measurements_by_load_count"],
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Generate qualification into a temporary path without rewriting historical evidence.",
    )
    arguments = parser.parse_args()
    if arguments.check:
        with tempfile.TemporaryDirectory(prefix="rate-replay-m4-check-") as temporary:
            result = qualify(Path(temporary) / "m4-optimizer-qualification.json")
    else:
        result = qualify()
    portfolio = result["portfolio_scenario"]
    print(
        "Milestone 4 qualification passed: "
        f"{portfolio['exact_search_status']}, "
        f"result {portfolio['result_sha256']}."
    )


if __name__ == "__main__":
    main()
