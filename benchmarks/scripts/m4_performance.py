#!/usr/bin/env python3
"""Execute frozen Milestone 4 optimization performance workloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from ratereplay_ingestion.simulated import load_locked_simulated_profile
from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ReferenceSlot,
    ScenarioElectricalConstraints,
    ScenarioInput,
)
from ratereplay_optimizer.results import build_scenario_result
from ratereplay_optimizer.scenario import (
    ScenarioValidationError,
    validate_and_decompose_scenario,
)
from ratereplay_optimizer.solver import (
    default_solver_configuration,
    optimize_exact,
    optimize_off_peak_heuristic,
)
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

ROOT = Path(__file__).resolve().parents[2]
V2_CHARTER = ROOT / "benchmarks/charters/performance-v2.json"
V2_WORKLOAD = ROOT / "benchmarks/workloads/m4-july-optimization.json"
V2_FAILURE = ROOT / "evidence/performance/m4-performance-v2-failed.json"
V3_CHARTER = ROOT / "benchmarks/charters/performance-v3.json"
V3_WORKLOAD = ROOT / "benchmarks/workloads/m4-july-optimization-v2.json"
V3_RESULT = ROOT / "evidence/performance/m4-optimization-performance-v3.json"


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_bytes()))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _facts() -> tuple[AccountFacts, DatedEligibilityFacts]:
    payload = _json(ROOT / "tariffs/examples/m3-comparison-account.json")
    return (
        AccountFacts.model_validate(payload["account_facts"]),
        DatedEligibilityFacts.model_validate(payload["dated_eligibility_facts"]),
    )


def _profile_slots() -> tuple[CanonicalProfileSlot, ...]:
    artifact = load_locked_simulated_profile(ROOT)
    return tuple(
        CanonicalProfileSlot(
            slot_start_utc=datetime.fromtimestamp(
                reading.start_utc_ns / 1_000_000_000,
                tz=UTC,
            ),
            duration_seconds=reading.duration_seconds,
            measured_energy_wh=reading.energy_wh,
        )
        for reading in artifact.content.readings
    )


def _scenario(workload: dict[str, Any], load_count: int) -> ScenarioInput:
    artifact = load_locked_simulated_profile(ROOT)
    slots = _profile_slots()
    template = cast(dict[str, Any], workload["load_template"])
    occurrence_template = cast(dict[str, Any], template["occurrence"])
    positive = {
        datetime.fromisoformat(cast(str, item[0]).replace("Z", "+00:00")): cast(int, item[1])
        for item in cast(list[list[object]], occurrence_template["positive_reference_slots"])
    }
    reference = tuple(
        ReferenceSlot(
            slot_start_utc=slot.slot_start_utc,
            duration_seconds=slot.duration_seconds,
            energy_wh=positive.get(slot.slot_start_utc, 0),
        )
        for slot in slots
    )
    required_energy = cast(int, occurrence_template["required_energy_wh"])
    if sum(slot.energy_wh for slot in reference) != required_energy:
        raise RuntimeError("M4_REFERENCE_ENERGY_MISMATCH")
    loads = tuple(
        FlexibleLoad(
            load_id=UUID(int=index + 1),
            physical_asset_key=f"benchmark-ev-{index + 1}",
            kind=cast(Any, template["kind"]),
            mode=cast(Any, template["mode"]),
            execution_spec=InterruptibleModulatingSpec(
                execution_type="INTERRUPTIBLE_MODULATING",
                maximum_power_w=cast(int, template["maximum_power_w"]),
                minimum_power_when_active_w=cast(int, template["minimum_power_when_active_w"]),
            ),
            occurrences=(
                LoadOccurrence(
                    occurrence_id=UUID(int=1_000 + index),
                    required_energy_wh=required_energy,
                    earliest_start_utc=datetime.fromisoformat(
                        cast(str, occurrence_template["earliest_start_utc"]).replace("Z", "+00:00")
                    ),
                    deadline_utc=datetime.fromisoformat(
                        cast(str, occurrence_template["deadline_utc"]).replace("Z", "+00:00")
                    ),
                    reference_schedule=reference,
                ),
            ),
        )
        for index in range(load_count)
    )
    constraints = cast(dict[str, Any], workload.get("electrical_constraints", {}))
    return ScenarioInput(
        scenario_version="historical-flex-scenario-v1",
        profile_content_sha256=artifact.content.sha256(),
        tariff_version_id=cast(list[str], workload["tariff_version_ids"])[-1],
        profile_slots=slots,
        loads=loads,
        electrical_constraints=ScenarioElectricalConstraints.model_validate(constraints),
    )


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def record_v2_failure() -> None:
    charter = _json(V2_CHARTER)
    workload = _json(V2_WORKLOAD)
    try:
        validate_and_decompose_scenario(_scenario(workload, 1))
    except ScenarioValidationError as error:
        if error.code != "NEGATIVE_FIXED_BACKGROUND":
            raise RuntimeError(f"UNEXPECTED_V2_FAILURE:{error.code}") from error
        result = {
            "schema_version": "performance-charter-failure-v1",
            "charter_version": charter["charter_version"],
            "charter_sha256": _sha256(V2_CHARTER),
            "workload_version": workload["workload_version"],
            "workload_sha256": _sha256(V2_WORKLOAD),
            "gate_result": "FAIL",
            "failed_phase": "PRE_SOLVE_REFERENCE_VALIDATION",
            "failure_code": error.code,
            "failure_message": str(error),
            "witness": error.witness,
            "reason": (
                "The frozen SHIFT_EXISTING reference declares energy that is not present "
                "in the measured simulated profile, so exact decomposition would create "
                "negative fixed background."
            ),
            "thresholds_changed_in_successor": False,
        }
        _write(V2_FAILURE, result)
        print(f"Preserved performance-v2 failure: {error.code}.")
        return
    raise RuntimeError("V2_WORKLOAD_UNEXPECTEDLY_VALID")


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def run_v3() -> None:
    charter = _json(V3_CHARTER)
    workload = _json(V3_WORKLOAD)
    expected_workload_hash = charter["workload_manifest"]["july_optimization_sha256"]
    if _sha256(V3_WORKLOAD) != expected_workload_hash:
        raise RuntimeError("V3_WORKLOAD_HASH_MISMATCH")
    account, dated = _facts()
    tariff_id = cast(list[str], workload["tariff_version_ids"])[-1]
    bundle = compile_tariff(
        ROOT,
        ROOT / f"tariffs/definitions/{tariff_id}.json",
    )
    configuration = default_solver_configuration(
        max_deterministic_time_per_stage=cast(
            float,
            cast(dict[str, Any], workload["solver_configuration"])[
                "max_deterministic_time_per_stage"
            ],
        )
    )
    repetitions = cast(int, charter["repetitions"]["optimization"])
    warmups = cast(int, charter["repetitions"]["warmups"])
    measurements: dict[str, Any] = {}
    for load_count in (1, 5):
        validated = validate_and_decompose_scenario(_scenario(workload, load_count))
        hashes: list[str] = []
        durations: list[float] = []
        for index in range(warmups + repetitions):
            started = time.perf_counter_ns()
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
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if exact.search_status != "OPTIMAL":
                raise RuntimeError(f"M4_EXACT_STATUS:{exact.search_status}")
            if result.exact.selected.verification.status != "VALID":
                raise RuntimeError("M4_SELECTED_SCHEDULE_UNVERIFIED")
            if index >= warmups:
                durations.append(elapsed_ms)
                hashes.append(result.result_sha256)
        if len(set(hashes)) != 1:
            raise RuntimeError(f"M4_NONDETERMINISTIC_RESULT:{load_count}")
        threshold_key = (
            "july_optimization_one_load_p95_ms"
            if load_count == 1
            else "july_optimization_five_load_p95_ms"
        )
        p95 = _nearest_rank(durations, 0.95)
        threshold = cast(float, charter["thresholds"][threshold_key])
        measurements[str(load_count)] = {
            "repetitions": repetitions,
            "warmups": warmups,
            "durations_ms": [round(value, 6) for value in durations],
            "p50_ms": round(_nearest_rank(durations, 0.50), 6),
            "p95_ms": round(p95, 6),
            "p99_ms": round(_nearest_rank(durations, 0.99), 6),
            "maximum_ms": round(max(durations), 6),
            "threshold_ms": threshold,
            "result_sha256": hashes[0],
            "deterministic": True,
            "passed": p95 <= threshold,
        }
    passed = all(cast(bool, item["passed"]) for item in measurements.values())
    payload = {
        "schema_version": "m4-optimization-performance-v1",
        "charter_version": charter["charter_version"],
        "charter_sha256": _sha256(V3_CHARTER),
        "workload_version": workload["workload_version"],
        "workload_sha256": _sha256(V3_WORKLOAD),
        "hardware": charter["hardware"],
        "runtime": {
            "python": platform.python_version(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "calculation_time_mode": "HISTORICAL_REPLAY",
        "historical_addition_label": "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST",
        "measurements_by_load_count": measurements,
        "duplicate_successful_results": 0,
        "worker_recovery_qualification": "PENDING_MILESTONE_5_DURABLE_SCENARIO_WORKER",
        "gate_result": "PASS" if passed else "FAIL",
    }
    _write(V3_RESULT, payload)
    if not passed:
        raise RuntimeError("M4_OPTIMIZATION_PERFORMANCE_FAILED")
    print("Milestone 4 optimization performance passed for one and five loads.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("record-v2-failure", "run-v3"),
    )
    args = parser.parse_args()
    if args.action == "record-v2-failure":
        record_v2_failure()
    else:
        run_v3()


if __name__ == "__main__":
    main()
