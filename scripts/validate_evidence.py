#!/usr/bin/env python3
"""Validate locked repository evidence without network access."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ratereplay_domain.energy import exact_watt_hours
from ratereplay_tariffs.admission import load_admitted_e1, load_admitted_tariff

ROOT = Path(__file__).resolve().parents[1]


class EvidenceValidationError(RuntimeError):
    """A locked evidence invariant does not hold."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


def _json(path: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ROOT / path).read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_external_sources() -> None:
    source_lock = _json("data/sources.lock.json")
    for source in source_lock["sources"]:
        artifact_path = source["artifact_path"]
        if artifact_path is not None:
            _require(
                _sha256(ROOT / artifact_path) == source["sha256"],
                f"SOURCE_HASH_MISMATCH:{source['source_id']}",
            )
    contract = _json("data/contracts/espi-admission-v1.json")
    _require(
        _sha256(ROOT / contract["schema_artifact"]) == contract["schema_sha256"],
        "ESPI_SCHEMA_HASH_MISMATCH",
    )
    _require(
        contract["fatal_nonintegral_code"] == "NON_INTEGRAL_WATT_HOUR",
        "ENERGY_ERROR_CODE_DRIFT",
    )


def _validate_csv() -> None:
    path = ROOT / "third_party/pge-csv/provider-sample.csv"
    payload = path.read_bytes()
    _require(payload.startswith(b"\xef\xbb\xbfName,SAMPLE"), "CSV_BOM_OR_REDACTION_MISMATCH")
    lines = payload.decode("utf-8-sig").splitlines()
    _require(
        lines[:4]
        == [
            "Name,SAMPLE",
            'Address,"SAMPLE"',
            "Account Number,SAMPLE",
            "Service,SAMPLE",
        ],
        "CSV_PROLOGUE_MISMATCH",
    )
    _require(
        lines[5] == "TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST,NOTES",
        "CSV_HEADER_MISMATCH",
    )
    reader = csv.DictReader(lines[5:])
    count = 0
    for row in reader:
        _require(row["TYPE"] == "Electric usage", "CSV_TYPE_MISMATCH")
        _require(row["UNITS"] == "kWh", "CSV_UNIT_MISMATCH")
        exact_watt_hours(row["USAGE"], source_unit="kWh", power_of_ten_multiplier=0)
        start = datetime.strptime(row["START TIME"], "%H:%M")
        end = datetime.strptime(row["END TIME"], "%H:%M") + timedelta(minutes=1)
        if end <= start:
            end += timedelta(days=1)
        _require(
            end - start in {timedelta(minutes=15), timedelta(hours=1)},
            "CSV_INTERVAL_DURATION_MISMATCH",
        )
        count += 1
    _require(count == 5_664, "CSV_ROW_COUNT_MISMATCH")


def _validate_tariffs() -> None:
    lock = _json("tariffs/sources.lock.json")
    source_ids = {source["source_id"] for source in lock["sources"]}
    _require(
        {"pge-advice-7921-e", "pge-advice-7846-e"} <= source_ids,
        "E1_STABLE_SOURCE_MISSING",
    )
    vectors = lock["tariff_component_vectors"]
    e1 = next(vector for vector in vectors if vector["tariff_id"] == "pge-e1-2026-07")
    _require(
        e1["service_window"] == ["2026-07-01", "2026-08-01"],
        "E1_SERVICE_WINDOW_MISMATCH",
    )
    _require(e1["coverage_count_per_service_instant"] == 1, "E1_VECTOR_COVERAGE_MISMATCH")
    _require(len(e1["components"]) == 2, "E1_COMPONENT_COUNT_MISMATCH")
    golden = _json("tariffs/golden/e1-july-2026-complete-bill.json")
    _require(set(golden["source_ids"]) <= source_ids, "E1_GOLDEN_SOURCE_MISSING")
    matrix = _json("tariffs/admission/candidate-matrix-v1.json")
    _require(len(matrix["tariffs"]) == 5, "CANDIDATE_COUNT_MISMATCH")
    statuses = {
        candidate["tariff_id"]: candidate["admission_status"] for candidate in matrix["tariffs"]
    }
    _require(statuses["E-1"] == "ADMITTED", "E1_ADMISSION_STATUS_MISMATCH")
    _require(statuses["E-TOU-C"] == "ADMITTED", "ETOUC_ADMISSION_STATUS_MISMATCH")
    _require(statuses["E-TOU-D"] == "ADMITTED", "ETOUD_ADMISSION_STATUS_MISMATCH")
    _require(statuses["E-ELEC"] == "ADMITTED", "EELEC_ADMISSION_STATUS_MISMATCH")
    _require(statuses["EV2-A"] == "ADMITTED", "EV2A_ADMISSION_STATUS_MISMATCH")
    _require(all(status == "ADMITTED" for status in statuses.values()), "TARIFF_NOT_ADMITTED")
    _require(e1["admission_status"] == "ADMITTED", "E1_COMPONENT_ADMISSION_MISMATCH")
    admission = _json("tariffs/admission/pge-e1-2026-07.json")
    _require(admission["admission_status"] == "ADMITTED", "E1_ADMISSION_LOCK_MISMATCH")
    _require(
        _sha256(ROOT / admission["definition"]["path"]) == admission["definition"]["sha256"],
        "E1_DEFINITION_HASH_MISMATCH",
    )
    for suite in admission["golden_suites"]:
        _require(
            _sha256(ROOT / suite["path"]) == suite["sha256"],
            "E1_GOLDEN_HASH_MISMATCH",
        )
    admitted = load_admitted_e1(ROOT)
    _require(
        admitted.compilation.compiler_content_sha256 == admission["compiler_content_sha256"],
        "E1_COMPILER_HASH_MISMATCH",
    )
    etouc = load_admitted_tariff(ROOT, "E-TOU-C")
    _require(
        etouc.compilation.compiler_content_sha256
        == "4ba9809c0a2d7ec65003c1cfb2eb7ad72b843e320b139bedc84926aeeb151101",
        "ETOUC_COMPILER_HASH_MISMATCH",
    )
    etoud = load_admitted_tariff(ROOT, "E-TOU-D")
    _require(
        etoud.compilation.compiler_content_sha256
        == "7b0315d0de599c7952f411299b83874350e039b2338fce8f58414289a5fce4e3",
        "ETOUD_COMPILER_HASH_MISMATCH",
    )
    eelec = load_admitted_tariff(ROOT, "E-ELEC")
    _require(
        eelec.compilation.compiler_content_sha256
        == "73d85e38aa547a3e7d12b172fcabb488369e00a92042d116a1f9443d6c5f7c00",
        "EELEC_COMPILER_HASH_MISMATCH",
    )
    ev2a = load_admitted_tariff(ROOT, "EV2-A")
    _require(
        ev2a.compilation.compiler_content_sha256
        == "b4c7921bbff209b6ddc7eadfe94c63ae9085ce223fc4f1e622d72ac61ee92e2a",
        "EV2A_COMPILER_HASH_MISMATCH",
    )
    holiday = _json("tariffs/calendars/ca-observed-holidays-2026.json")
    _require(
        holiday["holidays_used_in_july_window"]
        == [{"date": "2026-07-03", "name": "Independence Day observed"}],
        "JULY_HOLIDAY_LOCK_MISMATCH",
    )


def _validate_generated_evidence() -> None:
    profile_lock = _json("data/demo/profile.lock.json")
    _require(
        _sha256(ROOT / profile_lock["artifact_path"]) == profile_lock["artifact_sha256"],
        "DEMO_PROFILE_HASH_MISMATCH",
    )
    profile = _json(profile_lock["artifact_path"])
    _require(len(profile["readings"]) == 31 * 96, "DEMO_PROFILE_INTERVAL_COUNT_MISMATCH")
    _require(
        sum(reading["energy_wh"] for reading in profile["readings"]) == 750_000,
        "DEMO_PROFILE_ENERGY_MISMATCH",
    )
    _require(
        all(isinstance(reading["energy_wh"], int) for reading in profile["readings"]),
        "DEMO_PROFILE_NONINTEGER_ENERGY",
    )
    feasibility = _json("evidence/performance/m0-feasibility.json")
    _require(feasibility["passed"] is True, "M0_FEASIBILITY_FAILED")
    charter = _json("benchmarks/charters/performance-v1.json")
    _require(charter["duplicate_result_threshold"] == 0, "DUPLICATE_THRESHOLD_DRIFT")
    _require(
        charter["thresholds"]["worker_recovery_maximum_ms"] == 30_000,
        "RECOVERY_THRESHOLD_DRIFT",
    )
    allowlist = _json("artifacts/demo/allowlist.v1.json")
    schema = _json("artifacts/demo/manifest.schema.json")
    _require(len(allowlist["logical_artifact_ids"]) == 9, "DEMO_ALLOWLIST_COUNT_MISMATCH")
    _require(bool(allowlist["prohibited_capabilities"]), "DEMO_PROHIBITIONS_MISSING")
    _require(
        schema["properties"]["generation_command"]["const"] == "make demo-artifacts",
        "DEMO_GENERATION_COMMAND_DRIFT",
    )


def _validate_synthetic_study_qa() -> None:
    qa = _json("evidence/development/user-study/synthetic-protocol-qa.v1.json")
    _require(
        qa["evidence_class"] == "SYNTHETIC_PERSONA_DEVELOPMENT_ONLY",
        "SYNTHETIC_STUDY_CLASS_INVALID",
    )
    _require(qa["human_gate_eligible"] is False, "SYNTHETIC_STUDY_GATE_ELIGIBLE")
    _require(qa["genuine_participant_count"] == 0, "SYNTHETIC_STUDY_GENUINE_COUNT_INVALID")
    _require(qa["threshold_result"] == "NOT_EVALUATED", "SYNTHETIC_STUDY_THRESHOLD_INVALID")
    _require(len(qa["synthetic_personas"]) == 5, "SYNTHETIC_STUDY_PERSONA_COUNT_INVALID")
    _require(
        all(persona["persona_id"].startswith("SYN-") for persona in qa["synthetic_personas"]),
        "SYNTHETIC_STUDY_PERSONA_ID_INVALID",
    )
    _require(
        all(finding["status"] == "RESOLVED_AUTOMATED" for finding in qa["findings"]),
        "SYNTHETIC_STUDY_FINDING_UNRESOLVED",
    )
    _require(
        qa["disposition"]
        == {
            "synthetic_protocol_qa": "COMPLETED",
            "human_validation_state": "HUMAN_VALIDATION_DEFERRED",
            "human_threshold": "NOT_APPLICABLE",
            "may_count_toward_milestone_acceptance": False,
        },
        "SYNTHETIC_STUDY_DISPOSITION_INVALID",
    )


def _validate_m1_evidence() -> None:
    charter = _json("benchmarks/charters/performance-v1.json")
    recovery = _json("evidence/performance/m1-import-recovery.json")
    _require(recovery["passed"] is True, "M1_RECOVERY_FAILED")
    _require(recovery["charter_version"] == charter["charter_version"], "M1_CHARTER_DRIFT")
    _require(
        recovery["charter_sha256"] == _sha256(ROOT / "benchmarks/charters/performance-v1.json"),
        "M1_CHARTER_HASH_MISMATCH",
    )
    _require(
        recovery["fixture_sha256"]
        == _sha256(ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"),
        "M1_RECOVERY_FIXTURE_HASH_MISMATCH",
    )
    _require(
        recovery["schema_sha256"] == _sha256(ROOT / "third_party/espi-schema/espi-4.0.xsd"),
        "M1_RECOVERY_SCHEMA_HASH_MISMATCH",
    )
    _require(len(recovery["recovery_cases"]) == 10, "M1_RECOVERY_CASE_COUNT_MISMATCH")
    _require(
        {result["case"] for result in recovery["recovery_cases"]}
        == {"before_parse", "during_parse", "before_publish", "after_publish"},
        "M1_RECOVERY_CRASH_COVERAGE_MISMATCH",
    )
    _require(
        all(result["recovered"] is True for result in recovery["recovery_cases"]),
        "M1_RECOVERY_CASE_FAILED",
    )
    _require(recovery["lease_seconds"] == 20, "M1_LEASE_DURATION_DRIFT")
    _require(recovery["poll_seconds"] <= 1, "M1_WORKER_POLL_DRIFT")
    _require(
        recovery["maximum_recovery_upper_bound_ms"]
        <= charter["thresholds"]["worker_recovery_maximum_ms"],
        "M1_RECOVERY_THRESHOLD_FAILED",
    )
    _require(recovery["duplicate_draft_rows"] == 0, "M1_DUPLICATE_DRAFT")
    _require(recovery["duplicate_terminal_results"] == 0, "M1_DUPLICATE_TERMINAL")


def _validate_m4_performance_charter() -> None:
    charter = _json("benchmarks/charters/performance-v2.json")
    workload = _json("benchmarks/workloads/m4-july-optimization.json")
    manifest = charter["workload_manifest"]
    _require(
        charter["charter_version"] == "performance-acceptance-v2",
        "M4_CHARTER_VERSION_DRIFT",
    )
    _require(
        charter["supersedes"] == "benchmarks/charters/performance-v1.json",
        "M4_CHARTER_HISTORY_MISSING",
    )
    _require(
        manifest["july_optimization_sha256"] == _sha256(ROOT / manifest["july_optimization_path"]),
        "M4_OPTIMIZATION_WORKLOAD_HASH_MISMATCH",
    )
    _require(
        workload["profile_sha256"] == _sha256(ROOT / workload["profile_path"]),
        "M4_OPTIMIZATION_PROFILE_HASH_MISMATCH",
    )
    _require(manifest["optimization_load_counts"] == [0, 1, 5], "M4_LOAD_COUNTS_DRIFT")
    _require(
        charter["solver_limits"]
        == workload["solver_configuration"]
        | {
            "exact_objective_stages": 4,
            "heuristic_objective_stages": 2,
            "wall_clock_limit": None,
        },
        "M4_SOLVER_LIMIT_DRIFT",
    )
    _require(
        charter["thresholds"]["july_optimization_one_load_p95_ms"] == 10_000,
        "M4_OPTIMIZATION_THRESHOLD_DRIFT",
    )
    _require(
        charter["thresholds"]["scenario_worker_recovery_maximum_ms"] == 30_000,
        "M4_SCENARIO_RECOVERY_THRESHOLD_DRIFT",
    )
    failure = _json("evidence/performance/m4-performance-v2-failed.json")
    _require(failure["gate_result"] == "FAIL", "M4_V2_FAILURE_NOT_PRESERVED")
    _require(
        failure["charter_sha256"] == _sha256(ROOT / "benchmarks/charters/performance-v2.json"),
        "M4_V2_FAILURE_CHARTER_HASH_MISMATCH",
    )
    _require(
        failure["workload_sha256"]
        == _sha256(ROOT / "benchmarks/workloads/m4-july-optimization.json"),
        "M4_V2_FAILURE_WORKLOAD_HASH_MISMATCH",
    )
    _require(
        failure["failure_code"] == "NEGATIVE_FIXED_BACKGROUND",
        "M4_V2_FAILURE_CODE_DRIFT",
    )
    _require(
        failure["thresholds_changed_in_successor"] is False,
        "M4_V2_THRESHOLDS_RELABELED",
    )
    successor = _json("benchmarks/charters/performance-v3.json")
    successor_workload = _json("benchmarks/workloads/m4-july-optimization-v2.json")
    _require(
        successor["charter_version"] == "performance-acceptance-v3",
        "M4_V3_CHARTER_VERSION_DRIFT",
    )
    _require(
        successor["supersedes"] == "benchmarks/charters/performance-v2.json",
        "M4_V3_CHARTER_HISTORY_MISSING",
    )
    _require(
        successor["thresholds"] == charter["thresholds"],
        "M4_V3_THRESHOLD_CHANGED",
    )
    _require(
        successor["workload_manifest"]["july_optimization_sha256"]
        == _sha256(ROOT / successor["workload_manifest"]["july_optimization_path"]),
        "M4_V3_WORKLOAD_HASH_MISMATCH",
    )
    prior = successor["prior_failed_charters"]
    _require(len(prior) == 1, "M4_V3_PRIOR_FAILURE_COUNT_MISMATCH")
    _require(
        prior[0]["evidence_sha256"]
        == _sha256(ROOT / "evidence/performance/m4-performance-v2-failed.json"),
        "M4_V3_PRIOR_FAILURE_HASH_MISMATCH",
    )
    _require(
        successor_workload["load_template"]["mode"] == "HISTORICAL_ADDITION",
        "M4_V3_WORKLOAD_MODE_DRIFT",
    )
    _require(
        successor_workload["load_template"]["historical_addition_label"]
        == "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST",
        "M4_V3_COUNTERFACTUAL_LABEL_MISSING",
    )
    performance = _json("evidence/performance/m4-optimization-performance-v3.json")
    _require(performance["gate_result"] == "PASS", "M4_V3_PERFORMANCE_FAILED")
    _require(
        performance["charter_sha256"] == _sha256(ROOT / "benchmarks/charters/performance-v3.json"),
        "M4_V3_PERFORMANCE_CHARTER_HASH_MISMATCH",
    )
    _require(
        performance["workload_sha256"]
        == _sha256(ROOT / "benchmarks/workloads/m4-july-optimization-v2.json"),
        "M4_V3_PERFORMANCE_WORKLOAD_HASH_MISMATCH",
    )
    measurements = performance["measurements_by_load_count"]
    _require(set(measurements) == {"1", "5"}, "M4_V3_LOAD_MEASUREMENT_DRIFT")
    for load_count, measurement in measurements.items():
        _require(measurement["repetitions"] == 10, "M4_V3_REPETITION_DRIFT")
        _require(len(measurement["durations_ms"]) == 10, "M4_V3_DURATION_COUNT_DRIFT")
        _require(measurement["deterministic"] is True, "M4_V3_RESULT_NONDETERMINISTIC")
        _require(measurement["passed"] is True, "M4_V3_LOAD_THRESHOLD_FAILED")
        _require(
            measurement["p95_ms"] <= measurement["threshold_ms"],
            f"M4_V3_LOAD_{load_count}_P95_FAILED",
        )
    _require(
        performance["duplicate_successful_results"] == 0,
        "M4_V3_DUPLICATE_RESULT",
    )
    _require(
        performance["worker_recovery_qualification"]
        == "PENDING_MILESTONE_5_DURABLE_SCENARIO_WORKER",
        "M4_V3_WORKER_RECOVERY_SCOPE_DRIFT",
    )


def _validate_m4_correctness_evidence() -> None:
    qualification = _json("evidence/correctness/m4-optimizer-qualification.json")
    _require(qualification["gate_result"] == "PASS", "M4_QUALIFICATION_FAILED")
    inputs = qualification["inputs"]
    _require(
        inputs["workload_sha256"]
        == _sha256(ROOT / "benchmarks/workloads/m4-july-optimization-v2.json"),
        "M4_QUALIFICATION_WORKLOAD_HASH_MISMATCH",
    )
    _require(
        inputs["charter_sha256"] == _sha256(ROOT / "benchmarks/charters/performance-v3.json"),
        "M4_QUALIFICATION_CHARTER_HASH_MISMATCH",
    )
    portfolio = qualification["portfolio_scenario"]
    _require(
        portfolio["reference_validation_status"] == "VALID",
        "M4_REFERENCE_VALIDATION_FAILED",
    )
    _require(
        portfolio["exact_measured_reconstruction"] is True,
        "M4_RECONSTRUCTION_FAILED",
    )
    _require(portfolio["exact_search_status"] == "OPTIMAL", "M4_EXACT_NOT_OPTIMAL")
    _require(
        portfolio["highest_objective_stage_proved_optimal"] == 4,
        "M4_LEXICOGRAPHIC_STAGE_INCOMPLETE",
    )
    _require(
        portfolio["selected_verification_status"] == "VALID",
        "M4_SELECTED_SCHEDULE_UNVERIFIED",
    )
    _require(
        portfolio["heuristic_bill_optimality_claim"] is False,
        "M4_HEURISTIC_OPTIMALITY_OVERCLAIM",
    )
    _require(
        portfolio["repeatable_under_locked_environment"] is True,
        "M4_REPEATABILITY_FAILED",
    )
    oracle = qualification["independent_exhaustive_oracle"]
    _require(
        oracle["complete_final_optimum_set"] == [[70, 0, 0]],
        "M4_ORACLE_OPTIMUM_SET_DRIFT",
    )
    _require(
        oracle["returned_schedule_in_optimum_set"] is True,
        "M4_RETURNED_SCHEDULE_NOT_OPTIMAL",
    )
    _require(
        oracle["seeded_corruption"]["observed_code"] == "VERIFIER_ENERGY_CONSERVATION_FAILED",
        "M4_SEEDED_CORRUPTION_REASON_DRIFT",
    )
    _require(
        qualification["public_tariff_lowering"]["count"] == 5,
        "M4_OPTIMIZABLE_TARIFF_COUNT_DRIFT",
    )
    performance = qualification["performance"]
    _require(
        performance["evidence_sha256"]
        == _sha256(ROOT / "evidence/performance/m4-optimization-performance-v3.json"),
        "M4_QUALIFICATION_PERFORMANCE_HASH_MISMATCH",
    )


def _validate_m2_evidence() -> None:
    qualification = _json("evidence/correctness/m2-e1-qualification.json")
    complete_golden = _json("tariffs/golden/e1-july-2026-complete-bill.json")
    boundary_golden = _json("tariffs/golden/e1-july-2026-boundaries.json")
    _require(qualification["gate_result"] == "PASS", "M2_QUALIFICATION_FAILED")
    compilation = qualification["compilation"]
    _require(compilation["deterministic"] is True, "M2_COMPILATION_NONDETERMINISTIC")
    _require(
        compilation["compiler_content_sha256"]
        == "ae003e7717fbb8fa964aac75ba21efa737f4db54bdba2abcb90b1a22d81a0016",
        "M2_COMPILER_EVIDENCE_DRIFT",
    )
    _require(
        compilation["active_component_count_by_key"] == [1, 1],
        "M2_COMPONENT_COVERAGE_DRIFT",
    )
    goldens = qualification["goldens"]
    _require(
        goldens["complete_bill_sha256"]
        == _sha256(ROOT / "tariffs/golden/e1-july-2026-complete-bill.json"),
        "M2_COMPLETE_GOLDEN_DRIFT",
    )
    _require(
        goldens["boundary_suite_sha256"]
        == _sha256(ROOT / "tariffs/golden/e1-july-2026-boundaries.json"),
        "M2_BOUNDARY_GOLDEN_DRIFT",
    )
    _require(
        goldens["boundary_case_count"] == len(boundary_golden["cases"]),
        "M2_BOUNDARY_CASE_COUNT_DRIFT",
    )
    replay = qualification["replay"]
    _require(replay["deterministic"] is True, "M2_REPLAY_NONDETERMINISTIC")
    _require(
        replay["line_cents"] == complete_golden["expected"]["line_cents"],
        "M2_LINE_GOLDEN_MISMATCH",
    )
    _require(
        replay["supported_calculated_cents"] == complete_golden["expected"]["total_cents"],
        "M2_TOTAL_GOLDEN_MISMATCH",
    )
    _require(
        replay["line_total_cents"] == replay["supported_calculated_cents"],
        "M2_LINE_TOTAL_MISMATCH",
    )
    _require(replay["user_unsupported_cents"] == 200, "M2_UNSUPPORTED_LINE_HIDDEN")
    _require(replay["unexplained_residual_cents"] == 981, "M2_RESIDUAL_DRIFT")
    _require(bool(replay["reconciliation_input_sha256"]), "M2_RECONCILIATION_HASH_MISSING")
    _require(bool(replay["reconciliation_policy_sha256"]), "M2_POLICY_HASH_MISSING")
    invalids = qualification["deliberate_invalid_inputs"]
    _require(len(invalids) == 5, "M2_INVALID_CASE_COUNT_DRIFT")
    _require(all(item["passed"] is True for item in invalids), "M2_INVALID_CASE_FAILED")
    mutations = qualification["rate_and_boundary_mutations"]
    _require(len(mutations) == 10, "M2_MUTATION_COUNT_DRIFT")
    _require(all(item["passed"] is True for item in mutations), "M2_MUTATION_SURVIVED")
    _require(len(qualification["provenance"]) == 2, "M2_PROVENANCE_COUNT_DRIFT")


def _validate_m3_evidence() -> None:
    qualification = _json("evidence/correctness/m3-comparison-qualification.json")
    _require(qualification["gate_result"] == "PASS", "M3_QUALIFICATION_FAILED")
    inputs = qualification["inputs"]
    _require(
        inputs["profile_sha256"] == _sha256(ROOT / "data/demo/july-2026-simulated-profile.json"),
        "M3_PROFILE_HASH_MISMATCH",
    )
    _require(
        inputs["account_sha256"] == _sha256(ROOT / "tariffs/examples/m3-comparison-account.json"),
        "M3_ACCOUNT_HASH_MISMATCH",
    )
    _require(inputs["profile_energy_wh"] == 750_000, "M3_PROFILE_ENERGY_DRIFT")
    _require(inputs["interval_count"] == 2_976, "M3_INTERVAL_COUNT_DRIFT")
    admission = qualification["tariff_admission"]
    _require(admission["count"] == 5, "M3_ADMITTED_TARIFF_COUNT_DRIFT")
    expected_plans = {"E-1", "E-TOU-C", "E-TOU-D", "E-ELEC", "EV2-A"}
    historical_compiler_hashes = {
        "E-1": "ae003e7717fbb8fa964aac75ba21efa737f4db54bdba2abcb90b1a22d81a0016",
        "E-TOU-C": "4514eb416fbc697835c29cc393767c932b5e184bbcd0cdfc52e0c058fed56a04",
        "E-TOU-D": "5eb62747fb1f31e4d9d3d799619743a8e387373cf3b601b1e2c6656963b5edc2",
        "E-ELEC": "15d9ecca0b2ca03b475b9c493412423509529c089b13c473873ec59f9bc073b7",
        "EV2-A": "f81fb5d51b47e7cd64b07c0a104cfde00434e5e91b85fa65dbea3e96b740194c",
    }
    _require(set(admission["plan_codes"]) == expected_plans, "M3_ADMITTED_PLAN_DRIFT")
    for plan_code in expected_plans:
        admitted = load_admitted_tariff(ROOT, plan_code)
        _require(admitted.lock.scope.comparison_admitted is True, "M3_SCOPE_NOT_ADMITTED")
        _require(
            admission["compiler_content_sha256"][plan_code]
            == historical_compiler_hashes[plan_code],
            f"M3_COMPILER_HASH_DRIFT:{plan_code}",
        )
    for suite in admission["independent_golden_suites"].values():
        _require(
            suite["sha256"] == _sha256(ROOT / suite["path"]),
            "M3_GOLDEN_HASH_DRIFT",
        )
        _require(suite["complete_bill_rule_count"] > 0, "M3_GOLDEN_RULES_MISSING")
        _require(suite["boundary_case_count"] > 0, "M3_GOLDEN_BOUNDARIES_MISSING")
    comparison = qualification["comparison"]
    _require(comparison["deterministic"] is True, "M3_COMPARISON_NONDETERMINISTIC")
    _require(comparison["rankable"] is True, "M3_FROZEN_COMPARISON_BLOCKED")
    _require(
        set(comparison["candidate_eligibility"].values()) == {"ELIGIBLE"},
        "M3_FROZEN_ELIGIBILITY_DRIFT",
    )
    _require(
        comparison["candidate_cost_cents"]
        == {
            "E-1": 27_728,
            "E-ELEC": 30_278,
            "E-TOU-C": 30_253,
            "E-TOU-D": 26_021,
            "EV2-A": 26_890,
        },
        "M3_CANDIDATE_COST_DRIFT",
    )
    _require(
        comparison["ranked_tariff_version_ids"]
        == [
            "pge-etoud-2026-07",
            "pge-ev2a-2026-07",
            "pge-e1-2026-07",
            "pge-etouc-2026-07",
            "pge-eelec-2026-07",
        ],
        "M3_RANKING_DRIFT",
    )
    _require(
        comparison["winner_tariff_version_ids"] == ["pge-etoud-2026-07"],
        "M3_WINNER_DRIFT",
    )
    _require(
        comparison["savings_against_current_supported_cents"] == 1_707,
        "M3_SUPPORTED_SAVINGS_DRIFT",
    )
    _require(comparison["exclusions"] == [], "M3_UNEXPECTED_EXCLUSION")
    blocked = qualification["blocked_cases"]
    _require(
        blocked["missing_account_fact"]["observed_status"] == "UNKNOWN",
        "M3_MISSING_FACT_DID_NOT_YIELD_UNKNOWN",
    )
    for case in blocked.values():
        _require(case["passed"] is True, "M3_BLOCKED_CASE_FAILED")
        _require(case["savings_output"] is None, "M3_BLOCKED_SAVINGS_EMITTED")
    _require(
        blocked["coverage_mutation"]["observed_exclusion"] == "UNCLASSIFIED_ACTIVE_COMPONENT",
        "M3_COVERAGE_MUTATION_DRIFT",
    )
    _require(
        blocked["eligibility_mutation"]["observed_status"] == "INELIGIBLE",
        "M3_ELIGIBILITY_MUTATION_DRIFT",
    )
    separation = qualification["reconciliation_separation"]
    _require(separation["passed"] is True, "M3_RECONCILIATION_SEPARATION_FAILED")
    _require(
        separation["alternative_results_contain_forbidden_fields"] is False,
        "M3_ALTERNATIVE_RECONCILIATION_LEAK",
    )


def main() -> None:
    _validate_external_sources()
    _validate_csv()
    _validate_tariffs()
    _validate_generated_evidence()
    _validate_synthetic_study_qa()
    _validate_m1_evidence()
    _validate_m4_performance_charter()
    _validate_m4_correctness_evidence()
    _validate_m2_evidence()
    _validate_m3_evidence()
    print("Repository evidence locks are internally consistent.")


if __name__ == "__main__":
    main()
