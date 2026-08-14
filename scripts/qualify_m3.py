#!/usr/bin/env python3
"""Generate deterministic Milestone 3 comparable-plan qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ratereplay_tariffs.admission import AdmittedTariff, load_all_admitted_tariffs
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    ReplayRequest,
    UserUnsupportedLine,
    replay_compiled_tariff,
)
from ratereplay_tariffs.cli import app as tariff_cli
from ratereplay_tariffs.comparison import (
    ComparisonResult,
    compare_admitted_tariffs,
    load_required_component_keys,
)
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.schema import AccountFacts, ChargeComponentKey, DatedEligibilityFacts
from typer.testing import CliRunner

from scripts.validate_m3_goldens import GOLDEN_PATHS
from scripts.validate_m3_goldens import validate as validate_independent_goldens

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data/demo/july-2026-simulated-profile.json"
ACCOUNT = ROOT / "tariffs/examples/m3-comparison-account.json"
DEFAULT_OUTPUT = ROOT / "evidence/correctness/m3-comparison-qualification.json"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RuntimeError(f"QUALIFICATION_INPUT_NOT_OBJECT:{path.name}")
    return cast(dict[str, Any], value)


def _comparison_request() -> IntervalReplayRequest:
    profile = _load(PROFILE)
    account = _load(ACCOUNT)
    readings = cast(list[dict[str, Any]], profile["readings"])
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256=_file_sha256(PROFILE),
        account_facts=AccountFacts.model_validate_json(json.dumps(account["account_facts"])),
        energy_wh=cast(int, profile["total_energy_wh"]),
        intervals=tuple(
            ReplayInterval(
                start_utc_ns=int(
                    datetime.fromisoformat(
                        cast(str, reading["start_utc"]).replace("Z", "+00:00")
                    ).timestamp()
                )
                * 1_000_000_000,
                duration_seconds=cast(int, reading["duration_seconds"]),
                energy_wh=cast(int, reading["energy_wh"]),
            )
            for reading in readings
        ),
        dated_eligibility_facts=DatedEligibilityFacts.model_validate_json(
            json.dumps(account["dated_eligibility_facts"])
        ),
    )


def _run_public_cli(input_path: Path, output_path: Path) -> dict[str, Any]:
    result = CliRunner().invoke(
        tariff_cli,
        [
            "compare",
            str(input_path),
            "--root",
            str(ROOT),
            "--output",
            str(output_path),
        ],
    )
    if result.exit_code != 0:
        raise RuntimeError(f"QUALIFICATION_CLI_FAILED:{result.output}") from result.exception
    return _load(output_path)


def _candidate_costs(result: ComparisonResult) -> dict[str, int]:
    return {
        candidate.plan_code: candidate.alternative_plan.supported_calculated_cents
        for candidate in result.candidates
        if candidate.alternative_plan is not None
    }


def _coverage_mutation(
    request: IntervalReplayRequest,
    tariffs: tuple[AdmittedTariff, ...],
    required_components: tuple[ChargeComponentKey, ...],
) -> dict[str, object]:
    mutated = list(tariffs)
    index = next(
        index for index, tariff in enumerate(mutated) if tariff.lock.plan_code == "E-TOU-C"
    )
    admitted = mutated[index]
    normalized = dict(admitted.compilation.normalized_ast)
    declared = cast(list[str], normalized["comparison_component_keys"])
    normalized["comparison_component_keys"] = [
        component for component in declared if component != "baseline_adjustment"
    ]
    mutated[index] = admitted.model_copy(
        update={
            "compilation": admitted.compilation.model_copy(update={"normalized_ast": normalized})
        }
    )
    result = compare_admitted_tariffs(
        tuple(mutated),
        request,
        current_tariff_version_id="pge-e1-2026-07",
        required_component_keys=required_components,
    )
    exclusion = next(
        item
        for item in result.exclusions
        if item.tariff_version_id == "pge-etouc-2026-07"
        and item.code == "UNCLASSIFIED_ACTIVE_COMPONENT"
    )
    if result.rankable or result.savings_against_current_supported_cents is not None:
        raise RuntimeError("COVERAGE_MUTATION_DID_NOT_BLOCK")
    return {
        "mutation": "remove_etouc_baseline_adjustment_declaration",
        "observed_exclusion": exclusion.code,
        "component_key": exclusion.component_key,
        "rankable": result.rankable,
        "savings_output": result.savings_against_current_supported_cents,
        "passed": True,
    }


def _eligibility_mutation(
    directory: Path,
    request: IntervalReplayRequest,
    tariffs: tuple[AdmittedTariff, ...],
    required_components: tuple[ChargeComponentKey, ...],
) -> dict[str, object]:
    definition_path = ROOT / "tariffs/definitions/pge-ev2a-2026-07.json"
    payload = _load(definition_path)
    predicate = cast(dict[str, object], payload["eligibility_predicate"])
    predicate["maximum_annual_baseline_ratio_numerator"] = 2
    mutated_path = directory / "pge-ev2a-eligibility-mutated.json"
    mutated_path.write_text(json.dumps(payload), encoding="utf-8")
    mutated = list(tariffs)
    index = next(index for index, tariff in enumerate(mutated) if tariff.lock.plan_code == "EV2-A")
    mutated[index] = mutated[index].model_copy(
        update={"compilation": compile_tariff(ROOT, mutated_path)}
    )
    result = compare_admitted_tariffs(
        tuple(mutated),
        request,
        current_tariff_version_id="pge-e1-2026-07",
        required_component_keys=required_components,
    )
    ev2a = next(candidate for candidate in result.candidates if candidate.plan_code == "EV2-A")
    if ev2a.eligibility.status != "INELIGIBLE" or result.rankable:
        raise RuntimeError("ELIGIBILITY_MUTATION_DID_NOT_BLOCK")
    return {
        "mutation": "tighten_ev2a_annual_baseline_ratio",
        "observed_status": ev2a.eligibility.status,
        "observed_exclusions": [
            item.code
            for item in result.exclusions
            if item.tariff_version_id == ev2a.tariff_version_id
        ],
        "rankable": result.rankable,
        "savings_output": result.savings_against_current_supported_cents,
        "passed": True,
    }


def _unknown_facts_case(
    request: IntervalReplayRequest,
    tariffs: tuple[AdmittedTariff, ...],
    required_components: tuple[ChargeComponentKey, ...],
) -> dict[str, object]:
    dated = cast(DatedEligibilityFacts, request.dated_eligibility_facts)
    missing_usage = dated.model_copy(update={"annual_usage_wh": None})
    result = compare_admitted_tariffs(
        tariffs,
        request.model_copy(update={"dated_eligibility_facts": missing_usage}),
        current_tariff_version_id="pge-e1-2026-07",
        required_component_keys=required_components,
    )
    ev2a = next(candidate for candidate in result.candidates if candidate.plan_code == "EV2-A")
    if (
        ev2a.eligibility.status != "UNKNOWN"
        or result.rankable
        or result.ranked_tariff_version_ids
        or result.winner_tariff_version_ids
        or result.savings_against_current_supported_cents is not None
    ):
        raise RuntimeError("UNKNOWN_FACTS_DID_NOT_BLOCK")
    return {
        "omitted_fact": "annual_usage_wh",
        "observed_status": ev2a.eligibility.status,
        "reason_codes": list(ev2a.eligibility.reason_codes),
        "ranked_tariff_version_ids": list(result.ranked_tariff_version_ids),
        "winner_tariff_version_ids": list(result.winner_tariff_version_ids),
        "savings_output": result.savings_against_current_supported_cents,
        "passed": True,
    }


def _current_reconciliation(
    request: IntervalReplayRequest, tariffs: tuple[AdmittedTariff, ...]
) -> dict[str, object]:
    e1 = next(tariff for tariff in tariffs if tariff.lock.plan_code == "E-1")
    current_request = ReplayRequest(
        request_version="e1-replay-request-v1",
        profile_content_sha256=request.profile_content_sha256,
        account_facts=request.account_facts,
        energy_wh=request.energy_wh,
        current_bill_total_cents=30_000,
        user_unsupported_lines=(
            UserUnsupportedLine(
                line_item_key="local_tax",
                description="Current bill only",
                amount_cents=300,
            ),
        ),
    )
    current = replay_compiled_tariff(e1.compilation, current_request)
    if current.reconciliation is None:
        raise RuntimeError("CURRENT_RECONCILIATION_MISSING")
    return {
        "current_user_unsupported_cents": current.reconciliation.user_unsupported_cents,
        "current_unexplained_residual_cents": current.reconciliation.unexplained_residual_cents,
        "alternative_result_forbidden_fields": [
            "current_bill_total_cents",
            "reconciliation",
            "user_unsupported_lines",
        ],
        "alternative_results_contain_forbidden_fields": False,
        "passed": True,
    }


def qualify(output: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    validate_independent_goldens()
    request = _comparison_request()
    tariffs = load_all_admitted_tariffs(ROOT)
    required_components = tuple(load_required_component_keys(ROOT))
    with tempfile.TemporaryDirectory(prefix="rate-replay-m3-") as temporary:
        directory = Path(temporary)
        input_path = directory / "comparison-input.json"
        input_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
        first = _run_public_cli(input_path, directory / "comparison-first.json")
        second = _run_public_cli(input_path, directory / "comparison-second.json")
        if first != second:
            raise RuntimeError("COMPARISON_OUTPUT_NOT_DETERMINISTIC")
        result = compare_admitted_tariffs(
            tariffs,
            request,
            current_tariff_version_id="pge-e1-2026-07",
            required_component_keys=required_components,
        )
        coverage_mutation = _coverage_mutation(request, tariffs, required_components)
        eligibility_mutation = _eligibility_mutation(
            directory, request, tariffs, required_components
        )

    if not result.rankable or result.exclusions:
        raise RuntimeError("FROZEN_COMPARISON_NOT_RANKABLE")
    if first["comparison_sha256"] != result.comparison_sha256:
        raise RuntimeError("CLI_COMPARISON_HASH_MISMATCH")
    if any(candidate.eligibility.status != "ELIGIBLE" for candidate in result.candidates):
        raise RuntimeError("FROZEN_CANDIDATE_NOT_ELIGIBLE")
    for candidate in result.candidates:
        alternative = candidate.alternative_plan
        if alternative is None:
            raise RuntimeError("FROZEN_ALTERNATIVE_MISSING")
        keys = alternative.model_dump(mode="json").keys()
        if {"current_bill_total_cents", "reconciliation", "user_unsupported_lines"} & keys:
            raise RuntimeError("ALTERNATIVE_RECONCILIATION_LEAK")
        if any(item.status == "BLOCKED" for item in candidate.component_coverage):
            raise RuntimeError("FROZEN_COMPONENT_COVERAGE_BLOCKED")

    compiler_hashes = {
        tariff.lock.plan_code: tariff.compilation.compiler_content_sha256 for tariff in tariffs
    }
    golden_suites = {
        plan_code: {
            "path": str(path.relative_to(ROOT)),
            "sha256": _file_sha256(path),
            "complete_bill_rule_count": len(_load(path)["complete_bill"]["rule_ids"]),
            "boundary_case_count": len(_load(path)["boundary_cases"]),
        }
        for plan_code, path in GOLDEN_PATHS.items()
    }
    result_payload: dict[str, Any] = {
        "schema_version": "m3-comparison-qualification-v1",
        "milestone": 3,
        "gate_result": "PASS",
        "scope": "Five admitted July 2026 PG&E tariffs for the locked bundled Tier 3 EV account",
        "commands": {
            "qualification": "make qualification-m3",
            "comparison_cli": "uv run ratereplay-tariff compare <strict-interval-request.json>",
            "postgres_integration": "make integration-m3",
        },
        "inputs": {
            "profile_path": str(PROFILE.relative_to(ROOT)),
            "profile_sha256": _file_sha256(PROFILE),
            "profile_energy_wh": request.energy_wh,
            "interval_count": len(request.intervals),
            "account_path": str(ACCOUNT.relative_to(ROOT)),
            "account_sha256": _file_sha256(ACCOUNT),
            "service_window": request.account_facts.service_window.model_dump(mode="json"),
        },
        "tariff_admission": {
            "count": len(tariffs),
            "plan_codes": [tariff.lock.plan_code for tariff in tariffs],
            "compiler_content_sha256": compiler_hashes,
            "independent_golden_suites": golden_suites,
        },
        "comparison": {
            "deterministic": True,
            "comparison_sha256": result.comparison_sha256,
            "rankable": result.rankable,
            "required_component_keys": list(result.required_component_keys),
            "common_supported_component_keys": list(result.common_supported_component_keys),
            "candidate_eligibility": {
                candidate.plan_code: candidate.eligibility.status for candidate in result.candidates
            },
            "candidate_cost_cents": _candidate_costs(result),
            "ranked_tariff_version_ids": list(result.ranked_tariff_version_ids),
            "winner_tariff_version_ids": list(result.winner_tariff_version_ids),
            "savings_against_current_supported_cents": (
                result.savings_against_current_supported_cents
            ),
            "exclusions": [item.model_dump(mode="json") for item in result.exclusions],
        },
        "blocked_cases": {
            "missing_account_fact": _unknown_facts_case(request, tariffs, required_components),
            "coverage_mutation": coverage_mutation,
            "eligibility_mutation": eligibility_mutation,
        },
        "reconciliation_separation": _current_reconciliation(request, tariffs),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result_payload, indent=2) + "\n", encoding="utf-8")
    return result_payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Generate qualification into a temporary path without rewriting historical evidence.",
    )
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory(prefix="rate-replay-m3-check-") as temporary:
            result = qualify(Path(temporary) / "m3-comparison-qualification.json")
    else:
        result = qualify()
    comparison = result["comparison"]
    print(
        "Milestone 3 qualification passed: "
        f"{result['tariff_admission']['count']} tariffs, "
        f"winner {comparison['winner_tariff_version_ids'][0]}, "
        f"comparison {comparison['comparison_sha256']}."
    )


if __name__ == "__main__":
    main()
