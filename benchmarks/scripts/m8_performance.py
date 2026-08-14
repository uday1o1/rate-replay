#!/usr/bin/env python3
"""Run the frozen Milestone 8 local computation performance workloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import resource
import statistics
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from ratereplay_ingestion.normalize import normalize_pge_csv
from ratereplay_ingestion.pge_csv import parse_pge_csv
from ratereplay_ingestion.simulated import load_locked_simulated_profile
from ratereplay_optimizer.results import build_scenario_result
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import (
    default_solver_configuration,
    optimize_exact,
    optimize_off_peak_heuristic,
)
from ratereplay_reports.redacted import build_redacted_report
from ratereplay_tariffs.admission import AdmittedTariff, load_all_admitted_tariffs
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    replay_compiled_tariff,
)
from ratereplay_tariffs.comparison import compare_admitted_tariffs, load_required_component_keys
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

from benchmarks.scripts.m4_performance import _scenario as optimization_scenario

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "benchmarks/manifests/m8-evaluation-v1.json"
SCALING = ROOT / "benchmarks/workloads/m8-ingestion-scaling-v1.json"
OPTIMIZATION = ROOT / "benchmarks/workloads/m4-july-optimization-v2.json"
CORE_OUTPUT = ROOT / "evidence/evaluation/m8-performance-local-core.json"
VARIANCE_OUTPUT = ROOT / "evidence/evaluation/m8-performance-variance-followup.json"
PACIFIC = ZoneInfo("America/Los_Angeles")


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _profile(profile_id: str) -> dict[str, Any]:
    profiles = cast(list[dict[str, Any]], _json(SCALING)["profiles"])
    try:
        return next(item for item in profiles if item["profile_id"] == profile_id)
    except StopIteration as error:
        raise RuntimeError(f"M8_SCALE_PROFILE_UNKNOWN:{profile_id}") from error


def _synthetic_csv(interval_count: int) -> bytes:
    lines = [
        "\ufeffName,SYNTHETIC M8 ENGINEERING",
        "Address,SYNTHETIC",
        "Account Number,SYNTHETIC",
        "Service,SYNTHETIC",
        "",
        "TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST,NOTES",
    ]
    current = datetime(2025, 1, 1, tzinfo=UTC)
    for index in range(interval_count):
        local_start = current.astimezone(PACIFIC)
        local_end = local_start + timedelta(minutes=14)
        energy_wh = 125 + ((index * 17) % 251)
        lines.append(
            "Electric usage,"
            f"{local_start:%Y-%m-%d},{local_start:%H:%M},{local_end:%H:%M},"
            f"{energy_wh / 1000:.3f},kWh,,"
        )
        current += timedelta(minutes=15)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _readings_hash(readings: object) -> str:
    digest = hashlib.sha256(b"RateReplay.M8SyntheticIngestion.v1\0")
    for reading in cast(Any, readings):
        digest.update(
            f"{reading.start_utc_ns},{reading.duration_seconds},{reading.energy_wh}\n".encode(
                "ascii"
            )
        )
    return digest.hexdigest()


def _measure_import(profile_id: str) -> dict[str, Any]:
    profile = _profile(profile_id)
    payload = _synthetic_csv(cast(int, profile["interval_count"]))
    started = time.perf_counter_ns()
    draft = normalize_pge_csv(parse_pge_csv(payload))
    duration_ms = (time.perf_counter_ns() - started) / 1_000_000
    observed_hash = _readings_hash(draft.readings)
    if observed_hash != profile["canonical_readings_sha256"]:
        raise RuntimeError(f"M8_SCALE_HASH_MISMATCH:{profile_id}")
    return {
        "duration_ms": round(duration_ms, 6),
        "peak_rss_bytes": _peak_rss_bytes(),
        "reading_count": len(draft.readings),
        "canonical_readings_sha256": observed_hash,
        "source_bytes": len(payload),
    }


def _facts() -> tuple[AccountFacts, DatedEligibilityFacts]:
    payload = _json(ROOT / "tariffs/examples/m3-comparison-account.json")
    return (
        AccountFacts.model_validate_json(json.dumps(payload["account_facts"])),
        DatedEligibilityFacts.model_validate_json(json.dumps(payload["dated_eligibility_facts"])),
    )


def _request() -> IntervalReplayRequest:
    profile = load_locked_simulated_profile(ROOT).content
    account, dated = _facts()
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256=profile.sha256(),
        account_facts=account,
        energy_wh=sum(item.energy_wh for item in profile.readings),
        intervals=tuple(
            ReplayInterval(
                start_utc_ns=item.start_utc_ns,
                duration_seconds=item.duration_seconds,
                energy_wh=item.energy_wh,
            )
            for item in profile.readings
        ),
        dated_eligibility_facts=dated,
    )


def _tariff_subset(tariffs: tuple[AdmittedTariff, ...], count: int) -> tuple[AdmittedTariff, ...]:
    order = {
        "pge-e1-2026-07": 0,
        "pge-etoud-2026-07": 1,
        "pge-ev2a-2026-07": 2,
        "pge-etouc-2026-07": 3,
        "pge-eelec-2026-07": 4,
    }
    selected = tuple(sorted(tariffs, key=lambda item: order[item.lock.tariff_version_id]))
    return selected[:count]


def _build_report_source() -> object:
    from scripts.generate_demo_artifacts import _facts as demo_facts
    from scripts.generate_demo_artifacts import _scenario as demo_scenario

    account, dated = demo_facts()
    return demo_scenario(account, dated, load_all_admitted_tariffs(ROOT))


def _operation_context(operation: str, scale: int) -> Callable[[], str]:
    tariffs = load_all_admitted_tariffs(ROOT)
    request = _request()
    if operation == "replay":
        tariff = _tariff_subset(tariffs, 1)[0]

        def replay() -> str:
            value = replay_compiled_tariff(tariff.compilation, request)
            return value.manifest.calculation_sha256

        return replay
    if operation == "comparison":
        selected = _tariff_subset(tariffs, scale)
        required = load_required_component_keys(ROOT)

        def comparison() -> str:
            result = compare_admitted_tariffs(
                selected,
                request,
                current_tariff_version_id="pge-e1-2026-07",
                required_component_keys=required,
            )
            if not result.rankable:
                raise RuntimeError("M8_PERFORMANCE_COMPARISON_NOT_RANKABLE")
            return result.comparison_sha256

        return comparison
    if operation == "report":
        source = cast(Any, _build_report_source())

        def report() -> str:
            return build_redacted_report(source).report_sha256

        return report
    if operation == "optimization":
        workload = _json(OPTIMIZATION)
        scenario = optimization_scenario(workload, scale)
        validated = validate_and_decompose_scenario(scenario)
        account, dated = _facts()
        tariff = next(
            item for item in tariffs if item.lock.tariff_version_id == scenario.tariff_version_id
        )
        configuration = default_solver_configuration(max_deterministic_time_per_stage=5.0)

        def optimization() -> str:
            exact = optimize_exact(
                validated,
                tariff.compilation,
                account,
                dated_facts=dated,
                configuration=configuration,
            )
            heuristic = optimize_off_peak_heuristic(
                validated,
                tariff.compilation,
                account,
                dated_facts=dated,
                configuration=configuration,
            )
            result = build_scenario_result(
                validated,
                tariff.compilation,
                account,
                dated,
                exact,
                heuristic,
            )
            if (
                exact.search_status != "OPTIMAL"
                or result.exact.selected.verification.status != "VALID"
            ):
                raise RuntimeError("M8_PERFORMANCE_OPTIMIZATION_INVALID")
            return result.result_sha256

        return optimization
    raise RuntimeError(f"M8_PERFORMANCE_OPERATION_UNKNOWN:{operation}")


def _measure_operation(operation: str, scale: int) -> dict[str, Any]:
    callable_operation = _operation_context(operation, scale)
    started = time.perf_counter_ns()
    result_sha256 = callable_operation()
    duration_ms = (time.perf_counter_ns() - started) / 1_000_000
    return {"duration_ms": round(duration_ms, 6), "result_sha256": result_sha256}


def _child(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "benchmarks.scripts.m8_performance", *command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"M8_PERFORMANCE_CHILD_FAILED:{' '.join(command)}:{completed.stderr[-2000:]}"
        )
    return cast(dict[str, Any], json.loads(completed.stdout))


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _statistics(
    durations: list[float],
    *,
    threshold_ms: float | None,
    result_hashes: list[str],
) -> dict[str, Any]:
    mean = statistics.fmean(durations)
    coefficient = 0.0 if mean == 0 else statistics.pstdev(durations) / mean
    p95 = _nearest_rank(durations, 0.95)
    payload = {
        "durations_ms": [round(value, 6) for value in durations],
        "repetitions": len(durations),
        "p50_ms": round(_nearest_rank(durations, 0.50), 6),
        "p95_ms": round(p95, 6),
        "p99_ms": round(_nearest_rank(durations, 0.99), 6),
        "maximum_ms": round(max(durations), 6),
        "mean_ms": round(mean, 6),
        "coefficient_of_variation": round(coefficient, 6),
        "variance_investigation_required": coefficient > 0.20,
        "variance_note": (
            "Fast local operations are sensitive to scheduler noise; every frozen "
            "repetition is retained."
            if coefficient > 0.20
            else None
        ),
        "deterministic_result": len(set(result_hashes)) == 1,
        "result_sha256": result_hashes[0],
        "threshold_ms": threshold_ms,
        "passed": len(set(result_hashes)) == 1 and (threshold_ms is None or p95 <= threshold_ms),
    }
    return payload


def _import_series(
    profile_id: str, *, cache_state: str, repetitions: int, warmups: int
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    if cache_state == "COLD":
        observations = [_child(["child-import", profile_id]) for _ in range(repetitions)]
    else:
        for index in range(warmups + repetitions):
            observation = _measure_import(profile_id)
            if index >= warmups:
                observations.append(observation)
    profile = _profile(profile_id)
    durations = [cast(float, item["duration_ms"]) for item in observations]
    maximum_rss = max(cast(int, item["peak_rss_bytes"]) for item in observations)
    manifest = _json(MANIFEST)
    thresholds = cast(dict[str, float], manifest["performance"]["thresholds"])
    wall_threshold = (
        thresholds["import_wall_one_month_p95_ms"]
        if profile_id == "one-month-15-minute"
        else thresholds["import_wall_one_year_p95_ms"]
        if profile_id == "one-year-15-minute"
        else None
    )
    rss_threshold = (
        thresholds["import_peak_rss_one_month_bytes"]
        if profile_id == "one-month-15-minute"
        else thresholds["import_peak_rss_one_year_bytes"]
        if profile_id == "one-year-15-minute"
        else None
    )
    stats = _statistics(
        durations,
        threshold_ms=wall_threshold,
        result_hashes=[cast(str, item["canonical_readings_sha256"]) for item in observations],
    )
    stats.update(
        {
            "cache_state": cache_state,
            "profile_id": profile_id,
            "reading_count": profile["interval_count"],
            "source_bytes": observations[0]["source_bytes"],
            "maximum_peak_rss_bytes": maximum_rss,
            "peak_rss_threshold_bytes": rss_threshold,
            "memory_passed": rss_threshold is None or maximum_rss <= rss_threshold,
        }
    )
    stats["passed"] = stats["passed"] and stats["memory_passed"]
    return stats


def _operation_series(
    operation: str,
    scale: int,
    *,
    cache_state: str,
    repetitions: int,
    warmups: int,
    threshold_ms: float,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    if cache_state == "COLD":
        observations = [
            _child(["child-operation", operation, str(scale)]) for _ in range(repetitions)
        ]
    else:
        callable_operation = _operation_context(operation, scale)
        for index in range(warmups + repetitions):
            started = time.perf_counter_ns()
            result_sha256 = callable_operation()
            duration_ms = (time.perf_counter_ns() - started) / 1_000_000
            if index >= warmups:
                observations.append(
                    {
                        "duration_ms": round(duration_ms, 6),
                        "result_sha256": result_sha256,
                    }
                )
    stats = _statistics(
        [cast(float, item["duration_ms"]) for item in observations],
        threshold_ms=threshold_ms,
        result_hashes=[cast(str, item["result_sha256"]) for item in observations],
    )
    stats.update({"cache_state": cache_state, "operation": operation, "scale": scale})
    return stats


def validate_core(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "m8-performance-local-core-v1":
        raise RuntimeError("M8_PERFORMANCE_CORE_SCHEMA")
    if payload.get("artifact_sha256") != _artifact_hash(payload):
        raise RuntimeError("M8_PERFORMANCE_CORE_HASH")
    if payload.get("manifest_sha256") != _json(MANIFEST)["manifest_sha256"]:
        raise RuntimeError("M8_PERFORMANCE_CORE_MANIFEST")
    imports = cast(list[dict[str, Any]], payload.get("import_measurements"))
    operations = cast(list[dict[str, Any]], payload.get("operation_measurements"))
    if len(imports) != 6 or len(operations) != 12:
        raise RuntimeError("M8_PERFORMANCE_CORE_SERIES_COUNT")
    if not all(item.get("passed") is True for item in (*imports, *operations)):
        raise RuntimeError("M8_PERFORMANCE_CORE_GATE")
    if {item["cache_state"] for item in (*imports, *operations)} != {"COLD", "WARM"}:
        raise RuntimeError("M8_PERFORMANCE_CORE_CACHE_COVERAGE")


def validate_variance_followup(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "m8-performance-variance-followup-v1":
        raise RuntimeError("M8_PERFORMANCE_VARIANCE_SCHEMA")
    if payload.get("artifact_sha256") != _artifact_hash(payload):
        raise RuntimeError("M8_PERFORMANCE_VARIANCE_HASH")
    if payload.get("original_artifact_sha256") != _json(CORE_OUTPUT)["artifact_sha256"]:
        raise RuntimeError("M8_PERFORMANCE_VARIANCE_SOURCE")
    measurements = cast(list[dict[str, Any]], payload.get("measurements"))
    if len(measurements) != 3 or not all(item.get("passed") is True for item in measurements):
        raise RuntimeError("M8_PERFORMANCE_VARIANCE_GATE")


def run() -> dict[str, Any]:
    manifest = _json(MANIFEST)
    performance = cast(dict[str, Any], manifest["performance"])
    thresholds = cast(dict[str, float], performance["thresholds"])
    repetitions = cast(int, performance["measured_repetitions"])
    warmups = cast(int, performance["warmups"])
    import_measurements: list[dict[str, Any]] = []
    for profile in cast(list[dict[str, Any]], _json(SCALING)["profiles"]):
        for cache_state in ("COLD", "WARM"):
            print(
                f"M8_MEASURE import {profile['profile_id']} {cache_state}",
                file=sys.stderr,
                flush=True,
            )
            import_measurements.append(
                _import_series(
                    profile["profile_id"],
                    cache_state=cache_state,
                    repetitions=repetitions,
                    warmups=warmups,
                )
            )
    operation_specs = (
        ("replay", 1, thresholds["july_replay_p95_ms"]),
        ("comparison", 3, thresholds["july_comparison_uncached_p95_ms"]),
        ("comparison", 5, thresholds["july_comparison_uncached_p95_ms"]),
        ("report", 1, thresholds["report_generation_p95_ms"]),
        ("optimization", 1, thresholds["july_optimization_one_load_p95_ms"]),
        ("optimization", 5, thresholds["july_optimization_five_load_p95_ms"]),
    )
    operation_measurements: list[dict[str, Any]] = []
    for operation, scale, threshold in operation_specs:
        for cache_state in ("COLD", "WARM"):
            print(
                f"M8_MEASURE {operation} scale={scale} {cache_state}",
                file=sys.stderr,
                flush=True,
            )
            operation_measurements.append(
                _operation_series(
                    operation,
                    scale,
                    cache_state=cache_state,
                    repetitions=repetitions,
                    warmups=warmups,
                    threshold_ms=threshold,
                )
            )
    payload: dict[str, Any] = {
        "schema_version": "m8-performance-local-core-v1",
        "evidence_level": "LOCAL_REPRODUCIBLE",
        "evidence_scope": "PUBLIC_SIMULATED_AND_SYNTHETIC_ENGINEERING_ONLY",
        "manifest_sha256": manifest["manifest_sha256"],
        "generated_at": datetime.now(UTC).isoformat(),
        "hardware": manifest["hardware"],
        "runtime": {
            "python": platform.python_version(),
            "architecture": platform.machine(),
            "platform": platform.platform(),
        },
        "inputs": {
            "scaling_workload_sha256": _sha256(SCALING),
            "optimization_workload_sha256": _sha256(OPTIMIZATION),
            "simulated_profile_sha256": manifest["comparison"]["profile"]["sha256"],
        },
        "measurement_policy": {
            "cold": "fresh Python application process per measured repetition",
            "warm": "three warmups followed by ten retained repetitions in one process",
            "subprocess_startup_included": False,
            "setup_included": False,
            "failed_repetitions_omitted": False,
        },
        "import_measurements": import_measurements,
        "operation_measurements": operation_measurements,
        "zero_flexible_load_case": {
            "execution": "REFERENCE_REPLAY_ONLY",
            "measured_by": "replay scale 1",
            "solver_invoked": False,
            "passed": True,
        },
        "pending_release_topology_measurements": [
            "API comparison GET latency with concurrency 8",
            "API scenario GET latency with concurrency 8",
            "PostgreSQL database size",
            "S3-compatible object-store size",
            "scenario worker SIGKILL recovery",
        ],
        "limitations": [
            (
                "Ingestion scaling uses deterministic synthetic CSV and supports no "
                "customer-scale claim."
            ),
            "Timing excludes Python process startup and deterministic workload construction.",
            "Release-topology API, storage, and crash measurements are qualified separately.",
        ],
    }
    payload["gate_result"] = (
        "PASS"
        if all(
            item["passed"]
            for item in cast(
                list[dict[str, Any]],
                [*import_measurements, *operation_measurements],
            )
        )
        else "FAIL"
    )
    payload["artifact_sha256"] = _artifact_hash(payload)
    validate_core(payload)
    CORE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    CORE_OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_variance_followup() -> dict[str, Any]:
    original = _json(CORE_OUTPUT)
    validate_core(original)
    manifest = _json(MANIFEST)
    performance = cast(dict[str, Any], manifest["performance"])
    thresholds = cast(dict[str, float], performance["thresholds"])
    repetitions = cast(int, performance["measured_repetitions"])
    warmups = cast(int, performance["warmups"])
    measurements = [
        _import_series(
            "one-year-15-minute",
            cache_state="WARM",
            repetitions=repetitions,
            warmups=warmups,
        ),
        _operation_series(
            "replay",
            1,
            cache_state="COLD",
            repetitions=repetitions,
            warmups=warmups,
            threshold_ms=thresholds["july_replay_p95_ms"],
        ),
        _operation_series(
            "replay",
            1,
            cache_state="WARM",
            repetitions=repetitions,
            warmups=warmups,
            threshold_ms=thresholds["july_replay_p95_ms"],
        ),
    ]
    payload: dict[str, Any] = {
        "schema_version": "m8-performance-variance-followup-v1",
        "manifest_sha256": manifest["manifest_sha256"],
        "generated_at": datetime.now(UTC).isoformat(),
        "original_path": str(CORE_OUTPUT.relative_to(ROOT)),
        "original_artifact_sha256": original["artifact_sha256"],
        "trigger": "ORIGINAL_COEFFICIENT_OF_VARIATION_ABOVE_0_20",
        "original_results_preserved": True,
        "hypothesis": (
            "Host scheduling, memory allocation, and garbage-collection noise affect short local "
            "operations; deterministic output hashes distinguish timing noise from result drift."
        ),
        "measurements": measurements,
        "conclusion": (
            "Every follow-up repetition is retained, all output hashes are deterministic, and all "
            "frozen p95 and memory thresholds still pass."
        ),
        "gate_result": "PASS" if all(item["passed"] for item in measurements) else "FAIL",
    }
    payload["artifact_sha256"] = _artifact_hash(payload)
    validate_variance_followup(payload)
    VARIANCE_OUTPUT.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "run",
            "followup-variance",
            "check",
            "child-import",
            "child-operation",
        ),
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument("scale", nargs="?", type=int)
    arguments = parser.parse_args()
    if arguments.action == "child-import":
        if arguments.value is None:
            raise SystemExit("M8_CHILD_IMPORT_PROFILE_REQUIRED")
        print(json.dumps(_measure_import(arguments.value), sort_keys=True))
        return
    if arguments.action == "child-operation":
        if arguments.value is None or arguments.scale is None:
            raise SystemExit("M8_CHILD_OPERATION_ARGUMENT_REQUIRED")
        print(json.dumps(_measure_operation(arguments.value, arguments.scale), sort_keys=True))
        return
    if arguments.action == "check":
        validate_core(_json(CORE_OUTPUT))
        validate_variance_followup(_json(VARIANCE_OUTPUT))
        print("M8_PERFORMANCE_CORE_OK variance_followup=PASS")
        return
    if arguments.action == "followup-variance":
        payload = run_variance_followup()
        print(f"M8_PERFORMANCE_VARIANCE_FOLLOWUP_PASS measurements={len(payload['measurements'])}")
        return
    payload = run()
    print(
        "M8_PERFORMANCE_CORE_PASS "
        f"imports={len(payload['import_measurements'])} "
        f"operations={len(payload['operation_measurements'])}"
    )


if __name__ == "__main__":
    main()
