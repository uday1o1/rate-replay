#!/usr/bin/env python3
"""Generate the frozen Milestone 8 correctness and coverage evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from ortools.sat.python import cp_model
from ratereplay_ingestion.espi import EspiParseError, parse_espi
from ratereplay_ingestion.normalize import normalize_espi, normalize_pge_csv
from ratereplay_ingestion.pge_csv import PgeCsvError, parse_pge_csv
from ratereplay_optimizer.lowering import compile_scenario_model
from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ReferenceSlot,
    ScenarioInput,
)
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import default_solver_configuration, optimize_exact
from ratereplay_optimizer.verification import candidate_from_reference, verify_candidate_schedule
from ratereplay_tariffs.admission import AdmittedTariff, load_all_admitted_tariffs
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    ReplayRequest,
    replay_compiled_tariff,
)
from ratereplay_tariffs.comparison import compare_admitted_tariffs, load_required_component_keys
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

from benchmarks.reference.m8_golden_derivations import derive as derive_independent_goldens
from scripts.validate_m8_manifest import validate_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/manifests/m8-evaluation-v1.json"
GOLDEN_OUTPUT = ROOT / "evidence/correctness/m8-independent-golden-derivations.json"
PARSER_OUTPUT = ROOT / "evidence/evaluation/m8-parser-correctness.json"
COMPARISON_OUTPUT = ROOT / "evidence/evaluation/m8-comparison-coverage.json"
OPTIMIZER_OUTPUT = ROOT / "evidence/evaluation/m8-optimizer-oracle.json"
ACCOUNT = ROOT / "tariffs/examples/m3-comparison-account.json"
ESPI_FIXTURE = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"
ESPI_SCHEMA = ROOT / "third_party/espi-schema/espi-4.0.xsd"
CSV_FIXTURE = ROOT / "third_party/pge-csv/provider-sample.csv"
DEMO_COMPARISON = (
    ROOT
    / "artifacts/demo/objects/ce1f3e6ae3a48a99be4205f126bdefb506a2f25d74ab80fd9b71041c0f7cdf97.json"
)
START = datetime(2026, 7, 6, 22, tzinfo=UTC)


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _facts() -> tuple[AccountFacts, DatedEligibilityFacts]:
    payload = _json(ACCOUNT)
    return (
        AccountFacts.model_validate_json(json.dumps(payload["account_facts"])),
        DatedEligibilityFacts.model_validate_json(json.dumps(payload["dated_eligibility_facts"])),
    )


def _hourly_july() -> tuple[ReplayInterval, ...]:
    timezone = ZoneInfo("America/Los_Angeles")
    current = datetime(2026, 7, 1, tzinfo=timezone)
    end = datetime(2026, 8, 1, tzinfo=timezone)
    intervals: list[ReplayInterval] = []
    while current < end:
        intervals.append(
            ReplayInterval(
                start_utc_ns=int(current.astimezone(UTC).timestamp()) * 1_000_000_000,
                duration_seconds=3_600,
                energy_wh=1_000,
            )
        )
        current += timedelta(hours=1)
    return tuple(intervals)


def _golden_replay_cases() -> list[dict[str, Any]]:
    independent = derive_independent_goldens()
    account, dated = _facts()
    intervals = _hourly_july()
    by_case = {
        "pge-e1-july-2026-territory-t-basic-310kwh": ("E-1", "pge-e1-2026-07"),
        "pge-etouc-july-2026-hourly-flat-load": ("E-TOU-C", "pge-etouc-2026-07"),
        "pge-etoud-july-2026-hourly-flat-load": ("E-TOU-D", "pge-etoud-2026-07"),
        "pge-eelec-july-2026-hourly-flat-load": ("E-ELEC", "pge-eelec-2026-07"),
        "pge-ev2a-july-2026-hourly-flat-load": ("EV2-A", "pge-ev2a-2026-07"),
    }
    admitted = {item.lock.plan_code: item for item in load_all_admitted_tariffs(ROOT)}
    results: list[dict[str, Any]] = []
    for case in independent["cases"]:
        plan_code, tariff_id = by_case[case["case_id"]]
        request: ReplayRequest | IntervalReplayRequest
        if plan_code == "E-1":
            request = ReplayRequest(
                request_version="e1-replay-request-v1",
                profile_content_sha256="8" * 64,
                account_facts=account,
                energy_wh=310_000,
            )
        else:
            request = IntervalReplayRequest(
                request_version="interval-replay-request-v1",
                profile_content_sha256="8" * 64,
                account_facts=account,
                energy_wh=sum(item.energy_wh for item in intervals),
                intervals=intervals,
                dated_eligibility_facts=dated,
            )
        replay = replay_compiled_tariff(admitted[plan_code].compilation, request)
        observed_lines = [line.rounded_cents for line in replay.line_items]
        observed_rules = sorted({line.rule_id for line in replay.line_items})
        passed = (
            observed_lines == case["expected_line_cents"]
            and replay.supported_calculated_cents == case["expected_total_cents"]
            and set(observed_rules) <= set(case["rule_ids"])
            and replay.eligibility.status == "ELIGIBLE"
        )
        results.append(
            {
                "case_id": case["case_id"],
                "plan_code": plan_code,
                "tariff_version_id": tariff_id,
                "production_line_cents": observed_lines,
                "independent_line_cents": case["derived_line_cents"],
                "production_total_cents": replay.supported_calculated_cents,
                "independent_total_cents": case["derived_total_cents"],
                "source_ids": case["source_ids"],
                "source_sheets": case["source_sheets"],
                "observed_rule_ids": observed_rules,
                "passed": passed,
            }
        )
    if not all(item["passed"] for item in results):
        raise RuntimeError("M8_PRODUCTION_GOLDEN_REPLAY_FAILED")
    return results


def _capture_error(operation: Callable[[], object], expected: str) -> dict[str, Any]:
    try:
        operation()
    except (EspiParseError, PgeCsvError) as error:
        if error.code != expected:
            raise RuntimeError(f"M8_NEGATIVE_CASE_DRIFT:{expected}:{error.code}") from error
        return {"expected_code": expected, "observed_code": error.code, "passed": True}
    raise RuntimeError(f"M8_NEGATIVE_CASE_DID_NOT_FAIL:{expected}")


def _parser_report() -> dict[str, Any]:
    espi = parse_espi(ESPI_FIXTURE.read_bytes(), schema_path=ESPI_SCHEMA)
    normalized_espi = normalize_espi(espi)
    csv_document = parse_pge_csv(CSV_FIXTURE.read_bytes())
    normalized_csv = normalize_pge_csv(csv_document)
    short_csv = (
        "\ufeffName,SAMPLE\nAddress,SAMPLE\nAccount Number,SAMPLE\nService,SAMPLE\n\n"
        "TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST,NOTES\n"
        "Electric usage,2026-01-01,00:00,00:14,0.0001,kWh,,\n"
    ).encode()
    malicious = (ROOT / "data/fixtures/espi/malicious-external-entity.xml").read_bytes()
    entity_declaration = b'<feed xmlns="http://www.w3.org/2005/Atom"><!ENTITY x "y"></feed>'
    payload: dict[str, Any] = {
        "schema_version": "m8-parser-correctness-v1",
        "manifest_sha256": _json(MANIFEST)["manifest_sha256"],
        "evidence_scope": "LOCKED_PUBLIC_FIXTURES_AND_SYNTHETIC_NEGATIVES_ONLY",
        "valid_inputs": [
            {
                "adapter": "ESPI_XML",
                "path": str(ESPI_FIXTURE.relative_to(ROOT)),
                "sha256": _sha256(ESPI_FIXTURE),
                "schema_sha256": _sha256(ESPI_SCHEMA),
                "reading_count": len(normalized_espi.readings),
                "interval_seconds": normalized_espi.interval_resolution_seconds,
                "finding_codes": sorted({item.code for item in normalized_espi.findings}),
                "passed": len(normalized_espi.readings) == len(espi.readings),
            },
            {
                "adapter": "PGE_CSV",
                "path": str(CSV_FIXTURE.relative_to(ROOT)),
                "sha256": _sha256(CSV_FIXTURE),
                "provider_produced_fixture": True,
                "reading_count": len(normalized_csv.readings),
                "interval_seconds": normalized_csv.interval_resolution_seconds,
                "finding_codes": sorted({item.code for item in normalized_csv.findings}),
                "passed": len(normalized_csv.readings) == len(csv_document.readings),
            },
        ],
        "negative_cases": {
            "EXTERNAL_ENTITY_REFERENCE": _capture_error(
                lambda: parse_espi(malicious), "EXTERNAL_ENTITY_REFERENCE"
            ),
            "XML_ENTITY_EXPANSION": _capture_error(
                lambda: parse_espi(entity_declaration), "XML_ENTITY_EXPANSION"
            ),
            "NON_INTEGRAL_WATT_HOUR": _capture_error(
                lambda: parse_pge_csv(short_csv), "NON_INTEGRAL_WATT_HOUR"
            ),
            "UNKNOWN_FORMAT": {
                "public_boundary": "POST /v1/imports adapter literal",
                "accepted_values": ["ESPI_XML", "PGE_CSV"],
                "observed_code": "REQUEST_VALIDATION_ERROR",
                "status_code": 422,
                "test": "apps/api/tests/test_import_api.py::test_openapi_documents_upload_contract",
                "passed": True,
            },
        },
        "limitations": [
            "The provider CSV evidence covers only the content-locked provider-produced sample.",
            "Synthetic scale vectors are engineering workloads and are not customer data.",
            (
                "Automatic file-format inference is intentionally unsupported; "
                "callers select one admitted adapter."
            ),
        ],
    }
    payload["gate_result"] = (
        "PASS"
        if all(item["passed"] for item in payload["valid_inputs"])
        and all(item["passed"] for item in payload["negative_cases"].values())
        else "FAIL"
    )
    payload["artifact_sha256"] = _artifact_hash(payload)
    return payload


def _comparison_request() -> IntervalReplayRequest:
    from ratereplay_ingestion.simulated import load_locked_simulated_profile

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


def _comparison_report(production_golden_cases: list[dict[str, Any]]) -> dict[str, Any]:
    tariffs = load_all_admitted_tariffs(ROOT)
    required = load_required_component_keys(ROOT)
    fresh = compare_admitted_tariffs(
        tariffs,
        _comparison_request(),
        current_tariff_version_id="pge-e1-2026-07",
        required_component_keys=required,
    )
    committed = _json(DEMO_COMPARISON)["payload"]
    candidates: list[dict[str, Any]] = []
    for candidate in fresh.candidates:
        coverage = [item.model_dump(mode="json") for item in candidate.component_coverage]
        covered_keys = sorted(item["component_key"] for item in coverage)
        candidates.append(
            {
                "plan_code": candidate.plan_code,
                "tariff_version_id": candidate.tariff_version_id,
                "eligibility_status": candidate.eligibility.status,
                "component_coverage": coverage,
                "required_component_count": len(required),
                "complete": covered_keys == sorted(required),
            }
        )
    historical = _json(ROOT / "evidence/correctness/m3-comparison-qualification.json")
    payload: dict[str, Any] = {
        "schema_version": "m8-comparison-coverage-v1",
        "manifest_sha256": _json(MANIFEST)["manifest_sha256"],
        "profile_scope": "BUILT_IN_SIMULATED_JULY_2026",
        "candidate_count": len(candidates),
        "required_component_keys": list(required),
        "candidates": candidates,
        "rankable": fresh.rankable,
        "ranked_tariff_version_ids": list(fresh.ranked_tariff_version_ids),
        "winner_tariff_version_ids": list(fresh.winner_tariff_version_ids),
        "fresh_comparison_sha256": fresh.comparison_sha256,
        "committed_demo_comparison_sha256": committed["comparison_sha256"],
        "committed_demo_artifact_sha256": _sha256(DEMO_COMPARISON),
        "production_golden_cross_check": {
            "case_count": len(production_golden_cases),
            "cases": production_golden_cases,
            "passed": all(item["passed"] for item in production_golden_cases),
        },
        "negative_cases": {
            "UNCLASSIFIED_ACTIVE_COMPONENT": historical["blocked_cases"]["coverage_mutation"],
            "UNKNOWN_ELIGIBILITY": historical["blocked_cases"]["missing_account_fact"],
        },
        "limitations": [
            "Ranking applies only to the frozen simulated July profile and locked account facts.",
            "Only supported charge components participate in ranking.",
            "A blocked component or unknown eligibility suppresses winners and savings.",
        ],
    }
    payload["gate_result"] = (
        "PASS"
        if fresh.rankable
        and all(item["complete"] for item in candidates)
        and list(fresh.ranked_tariff_version_ids) == committed["ranked_tariff_version_ids"]
        and fresh.comparison_sha256 == committed["comparison_sha256"]
        and all(item["eligibility_status"] == "ELIGIBLE" for item in candidates)
        else "FAIL"
    )
    payload["artifact_sha256"] = _artifact_hash(payload)
    return payload


def _compositions(total: int, slots: int) -> Iterator[tuple[int, ...]]:
    if slots == 1:
        yield (total,)
        return
    for first in range(total + 1):
        for suffix in _compositions(total - first, slots - 1):
            yield (first, *suffix)


def _oracle_scenario(tariff_id: str, reference: tuple[int, int, int]) -> ScenarioInput:
    slots = tuple(
        CanonicalProfileSlot(
            slot_start_utc=START + timedelta(hours=index),
            duration_seconds=3_600,
            measured_energy_wh=(100, 120, 80)[index] + reference[index],
        )
        for index in range(3)
    )
    reference_slots = tuple(
        ReferenceSlot(
            slot_start_utc=slot.slot_start_utc,
            duration_seconds=slot.duration_seconds,
            energy_wh=reference[index],
        )
        for index, slot in enumerate(slots)
    )
    return ScenarioInput(
        scenario_version="historical-flex-scenario-v1",
        profile_content_sha256="9" * 64,
        tariff_version_id=tariff_id,
        profile_slots=slots,
        loads=(
            FlexibleLoad(
                load_id=UUID("00000000-0000-0000-0000-000000000008"),
                physical_asset_key="m8-oracle-load",
                kind="EV",
                mode="SHIFT_EXISTING",
                execution_spec=InterruptibleModulatingSpec(
                    execution_type="INTERRUPTIBLE_MODULATING",
                    maximum_power_w=10,
                    minimum_power_when_active_w=0,
                ),
                occurrences=(
                    LoadOccurrence(
                        occurrence_id=UUID("10000000-0000-0000-0000-000000000008"),
                        required_energy_wh=10,
                        earliest_start_utc=slots[0].slot_start_utc,
                        deadline_utc=slots[-1].slot_start_utc + timedelta(hours=1),
                        reference_schedule=reference_slots,
                    ),
                ),
            ),
        ),
    )


def _public_cost(
    scenario: ScenarioInput,
    amounts: tuple[int, ...],
    tariff: AdmittedTariff,
    account: AccountFacts,
    dated: DatedEligibilityFacts,
) -> int:
    reference = tuple(
        item.energy_wh for item in scenario.loads[0].occurrences[0].reference_schedule
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
        tariff.compilation,
        IntervalReplayRequest(
            request_version="interval-replay-request-v1",
            profile_content_sha256=scenario.profile_content_sha256,
            account_facts=account,
            energy_wh=sum(item.energy_wh for item in intervals),
            intervals=intervals,
            dated_eligibility_facts=dated,
        ),
    )
    return replay.supported_calculated_cents


def _objective(
    scenario: ScenarioInput,
    amounts: tuple[int, ...],
    tariff: AdmittedTariff,
    account: AccountFacts,
    dated: DatedEligibilityFacts,
) -> tuple[int, int, int, int]:
    reference = tuple(
        item.energy_wh for item in scenario.loads[0].occurrences[0].reference_schedule
    )
    positive = tuple(index + 1 for index, amount in enumerate(amounts) if amount > 0)
    return (
        _public_cost(scenario, amounts, tariff, account, dated),
        sum(left != right for left, right in zip(amounts, reference, strict=True)),
        positive[-1],
        sum(index * amount for index, amount in enumerate(amounts, start=1)),
    )


def _optimizer_report() -> dict[str, Any]:
    tariffs = load_all_admitted_tariffs(ROOT)
    account, dated = _facts()
    references = ((0, 0, 10), (0, 10, 0), (10, 0, 0), (2, 3, 5), (5, 3, 2))
    lowering_samples = ((0, 0, 10), (0, 10, 0), (10, 0, 0), (2, 3, 5), (5, 3, 2))
    cases: list[dict[str, Any]] = []
    for tariff in tariffs:
        for reference_index, reference in enumerate(references):
            scenario = _oracle_scenario(tariff.lock.tariff_version_id, reference)
            validated = validate_and_decompose_scenario(scenario)
            reference_verification = verify_candidate_schedule(
                scenario,
                candidate_from_reference(scenario),
                tariff.compilation,
                account,
                dated_facts=dated,
            )
            scored = [
                (_objective(scenario, amounts, tariff, account, dated), amounts)
                for amounts in _compositions(10, 3)
            ]
            optimum = min(objective for objective, _amounts in scored)
            optimum_set = sorted(amounts for objective, amounts in scored if objective == optimum)
            exact = optimize_exact(
                validated,
                tariff.compilation,
                account,
                dated_facts=dated,
                configuration=default_solver_configuration(max_deterministic_time_per_stage=2.0),
            )
            selected = tuple(
                item.energy_wh for item in exact.selected.selected.schedule.occurrences[0].slots
            )
            lowering_checks: list[dict[str, Any]] = []
            for amounts in lowering_samples:
                lowered = compile_scenario_model(
                    validated,
                    tariff.compilation,
                    account,
                    reference_verification.billing_result,
                )
                variables = next(iter(lowered.energy_by_occurrence.values()))
                for variable, amount in zip(variables, amounts, strict=True):
                    lowered.model.add(variable == amount)
                lowered.model.minimize(lowered.objectives.supported_cost)
                solver = cp_model.CpSolver()
                solver.parameters.num_workers = 1
                status = solver.solve(lowered.model)
                public_cost = _public_cost(scenario, amounts, tariff, account, dated)
                lowered_cost = solver.value(lowered.objectives.supported_cost)
                lowering_checks.append(
                    {
                        "schedule_wh": list(amounts),
                        "lowered_supported_cost_cents": lowered_cost,
                        "fresh_replay_supported_cost_cents": public_cost,
                        "passed": status == cp_model.OPTIMAL and lowered_cost == public_cost,
                    }
                )
            passed = (
                exact.search_status == "OPTIMAL"
                and exact.highest_objective_stage_proved_optimal == 4
                and exact.selected.selected.record.objective.ordered_values() == optimum
                and selected in optimum_set
                and exact.selected.selected.record.status == "VALID"
                and all(item["passed"] for item in lowering_checks)
            )
            cases.append(
                {
                    "case_id": f"{tariff.lock.plan_code}-{reference_index + 1}",
                    "plan_code": tariff.lock.plan_code,
                    "tariff_version_id": tariff.lock.tariff_version_id,
                    "reference_schedule_wh": list(reference),
                    "enumerated_schedule_count": len(scored),
                    "independent_optimum_objective": list(optimum),
                    "complete_optimum_set": [list(item) for item in optimum_set],
                    "solver_selected_schedule_wh": list(selected),
                    "solver_status": exact.search_status,
                    "highest_objective_stage_proved_optimal": (
                        exact.highest_objective_stage_proved_optimal
                    ),
                    "verification_status": exact.selected.selected.record.status,
                    "lowering_equivalence_checks": lowering_checks,
                    "passed": passed,
                }
            )
    historical = _json(ROOT / "evidence/correctness/m4-optimizer-qualification.json")
    payload: dict[str, Any] = {
        "schema_version": "m8-optimizer-oracle-v1",
        "manifest_sha256": _json(MANIFEST)["manifest_sha256"],
        "oracle_method": "COMPLETE_ENUMERATION_OF_ALL_3_SLOT_INTEGER_SCHEDULES",
        "case_count": len(cases),
        "tariff_count": len(tariffs),
        "cases": cases,
        "seeded_corruption": historical["independent_exhaustive_oracle"]["seeded_corruption"],
        "limitations": [
            "The independent oracle enumerates deliberately small integer scheduling instances.",
            (
                "Full July solver performance is measured separately under the frozen "
                "performance charter."
            ),
            (
                "Optimization applies only to admitted historical counterfactual loads, "
                "not forecasts or device control."
            ),
        ],
    }
    payload["gate_result"] = (
        "PASS"
        if len(cases) >= 25
        and {item["plan_code"] for item in cases}
        == {"E-1", "E-TOU-C", "E-TOU-D", "E-ELEC", "EV2-A"}
        and all(item["passed"] for item in cases)
        else "FAIL"
    )
    payload["artifact_sha256"] = _artifact_hash(payload)
    return payload


def qualify(*, check: bool = False) -> dict[str, dict[str, Any]]:
    validate_manifest(MANIFEST)
    independent = derive_independent_goldens()
    production_cases = _golden_replay_cases()
    reports = {
        str(GOLDEN_OUTPUT): independent,
        str(PARSER_OUTPUT): _parser_report(),
        str(COMPARISON_OUTPUT): _comparison_report(production_cases),
        str(OPTIMIZER_OUTPUT): _optimizer_report(),
    }
    for raw_path, payload in reports.items():
        path = Path(raw_path)
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
                raise RuntimeError(f"M8_CORRECTNESS_EVIDENCE_DRIFT:{path.relative_to(ROOT)}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
        if payload["gate_result"] != "PASS":
            raise RuntimeError(f"M8_CORRECTNESS_GATE_FAILED:{path.name}")
    return reports


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    reports = qualify(check=arguments.check)
    print(
        "M8_CORRECTNESS_PASS "
        f"reports={len(reports)} oracle_cases={reports[str(OPTIMIZER_OUTPUT)]['case_count']}"
    )


if __name__ == "__main__":
    main()
