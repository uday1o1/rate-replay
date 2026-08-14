#!/usr/bin/env python3
"""Generate deterministic Milestone 2 tariff and replay qualification evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ratereplay_tariffs.billing import ReplayError, ReplayRequest, replay_compiled_tariff
from ratereplay_tariffs.cli import app as tariff_cli
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "tariffs/definitions/pge-e1-2026-07.json"
INPUT = ROOT / "tariffs/examples/e1-replay-input.json"
COMPLETE_GOLDEN = ROOT / "tariffs/golden/e1-july-2026-complete-bill.json"
BOUNDARY_GOLDEN = ROOT / "tariffs/golden/e1-july-2026-boundaries.json"
DEFAULT_OUTPUT = ROOT / "evidence/correctness/m2-e1-qualification.json"

Mutation = Callable[[dict[str, Any]], None]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _content_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_cli(arguments: list[str], output: Path) -> dict[str, Any]:
    runner = CliRunner()
    result = runner.invoke(
        tariff_cli,
        [*arguments, "--root", str(ROOT), "--output", str(output)],
    )
    if result.exit_code != 0:
        raise RuntimeError(f"QUALIFICATION_CLI_FAILED:{result.output}") from result.exception
    value = json.loads(output.read_bytes())
    if not isinstance(value, dict):
        raise RuntimeError("QUALIFICATION_CLI_OUTPUT_INVALID")
    return value


def _write_mutation(directory: Path, name: str, mutation: Mutation) -> Path:
    payload = json.loads(DEFINITION.read_bytes())
    mutation(payload)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _duplicate_component(payload: dict[str, Any]) -> None:
    duplicate = copy.deepcopy(payload["component_versions"][0])
    duplicate["component_version_id"] = "duplicate-component"
    payload["component_versions"].append(duplicate)


def _invalid_cases() -> tuple[tuple[str, str, Mutation], ...]:
    return (
        (
            "component_gap",
            "COMPONENT_COVERAGE_GAP",
            lambda payload: payload["component_versions"][0]["effective_range"].update(
                {"start": "2026-07-02"}
            ),
        ),
        ("component_overlap", "COMPONENT_OVERLAP", _duplicate_component),
        (
            "invalid_tier",
            "INVALID_TIER",
            lambda payload: payload["charge_rules"][1]["tiers"][-1].update(
                {"upper_bound_kind": "BASELINE_ALLOWANCE"}
            ),
        ),
        (
            "unknown_unit",
            "UNKNOWN_UNIT",
            lambda payload: payload["charge_rules"][1].update({"rate_unit": "cents/therm"}),
        ),
        (
            "source_mismatch",
            "SOURCE_HASH_MISMATCH",
            lambda payload: payload["component_versions"][0]["source"].update(
                {"source_sha256": "0" * 64}
            ),
        ),
    )


def _rate_and_boundary_mutations() -> tuple[tuple[str, Mutation], ...]:
    return (
        (
            "tier_1_rate",
            lambda payload: payload["charge_rules"][1]["tiers"][0].update(
                {
                    "rate_microdollars_per_kwh": payload["charge_rules"][1]["tiers"][0][
                        "rate_microdollars_per_kwh"
                    ]
                    + 10_000
                }
            ),
        ),
        (
            "tier_2_rate",
            lambda payload: payload["charge_rules"][1]["tiers"][1].update(
                {
                    "rate_microdollars_per_kwh": payload["charge_rules"][1]["tiers"][1][
                        "rate_microdollars_per_kwh"
                    ]
                    + 10_000
                }
            ),
        ),
        (
            "daily_rate",
            lambda payload: payload["charge_rules"][2].update(
                {
                    "rate_microdollars_per_day": payload["charge_rules"][2][
                        "rate_microdollars_per_day"
                    ]
                    + 10_000
                }
            ),
        ),
        (
            "credit_amount",
            lambda payload: payload["charge_rules"][3].update(
                {"amount_microdollars": payload["charge_rules"][3]["amount_microdollars"] + 10_000}
            ),
        ),
        (
            "baseline_boundary",
            lambda payload: payload["charge_rules"][0].update(
                {"daily_allowance_wh": payload["charge_rules"][0]["daily_allowance_wh"] + 1_000}
            ),
        ),
        (
            "tier_boundary",
            lambda payload: payload["charge_rules"][1]["tiers"][0].update(
                {"upper_bound_numerator": 2}
            ),
        ),
        (
            "credit_month_boundary",
            lambda payload: payload["charge_rules"][3]["applicability"].update(
                {"bill_cycle_months": [7]}
            ),
        ),
        (
            "income_applicability",
            lambda payload: payload["charge_rules"][2]["applicability"].update(
                {"income_tiers": ["TIER_2"]}
            ),
        ),
        (
            "baseline_applicability",
            lambda payload: payload["charge_rules"][0]["applicability"].update(
                {"baseline_territories": ["Q"]}
            ),
        ),
        (
            "effective_date_boundary",
            lambda payload: payload["charge_rules"][1]["effective_range"].update(
                {"start": "2026-07-02"}
            ),
        ),
    )


def _invalid_results(directory: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for name, expected_code, mutation in _invalid_cases():
        path = _write_mutation(directory, name, mutation)
        observed_code = "NO_ERROR"
        try:
            compile_tariff(ROOT, path)
        except TariffCompileError as error:
            observed_code = error.code
        if observed_code != expected_code:
            raise RuntimeError(f"{name}: expected {expected_code}, observed {observed_code}")
        results.append(
            {
                "case": name,
                "expected_code": expected_code,
                "observed_code": observed_code,
                "passed": True,
            }
        )
    return results


def _mutation_results(
    directory: Path,
    request: ReplayRequest,
    expected_lines: list[int],
    expected_baseline_wh: int,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for name, mutation in _rate_and_boundary_mutations():
        path = _write_mutation(directory, name, mutation)
        failure_mode = "NO_FAILURE"
        try:
            replay = replay_compiled_tariff(compile_tariff(ROOT, path), request)
            lines = [line.rounded_cents for line in replay.line_items]
            if (
                lines != expected_lines
                or replay.manifest.baseline_allowance_wh != expected_baseline_wh
            ):
                failure_mode = "GOLDEN_MISMATCH"
        except (ReplayError, TariffCompileError) as error:
            failure_mode = error.code
        if failure_mode == "NO_FAILURE":
            raise RuntimeError(f"{name}: mutation did not break its intended golden")
        results.append({"mutation": name, "failure_mode": failure_mode, "passed": True})
    return results


def qualify(output: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    complete_golden = json.loads(COMPLETE_GOLDEN.read_bytes())
    boundary_golden = json.loads(BOUNDARY_GOLDEN.read_bytes())
    request = ReplayRequest.model_validate_json(INPUT.read_bytes())
    expected_lines = list(complete_golden["expected"]["line_cents"])
    expected_total = complete_golden["expected"]["total_cents"]
    expected_baseline_wh = complete_golden["inputs"]["baseline_wh"]

    with tempfile.TemporaryDirectory(prefix="rate-replay-m2-") as temporary:
        directory = Path(temporary)
        compile_first = _run_cli(["compile-e1"], directory / "compile-first.json")
        compile_second = _run_cli(["compile-e1"], directory / "compile-second.json")
        replay_first = _run_cli(["replay-e1", str(INPUT)], directory / "replay-first.json")
        replay_second = _run_cli(["replay-e1", str(INPUT)], directory / "replay-second.json")
        if compile_first != compile_second:
            raise RuntimeError("COMPILER_OUTPUT_NOT_DETERMINISTIC")
        if replay_first != replay_second:
            raise RuntimeError("REPLAY_OUTPUT_NOT_DETERMINISTIC")

        line_cents = [line["rounded_cents"] for line in replay_first["line_items"]]
        if line_cents != expected_lines:
            raise RuntimeError("COMPLETE_BILL_LINE_GOLDEN_MISMATCH")
        if replay_first["supported_calculated_cents"] != expected_total:
            raise RuntimeError("COMPLETE_BILL_TOTAL_GOLDEN_MISMATCH")
        if sum(line_cents) != replay_first["supported_calculated_cents"]:
            raise RuntimeError("LINE_TOTAL_MISMATCH")

        invalid_results = _invalid_results(directory)
        mutation_results = _mutation_results(
            directory, request, expected_lines, expected_baseline_wh
        )

    reports = compile_first["reports"]
    reconciliation = replay_first["reconciliation"]
    if reconciliation is None:
        raise RuntimeError("RECONCILIATION_MISSING")
    result: dict[str, Any] = {
        "schema_version": "m2-e1-qualification-v1",
        "milestone": 2,
        "gate_result": "PASS",
        "scope": "July 2026 PG&E E-1 historical replay for the locked target account only",
        "commands": {
            "qualification": "make qualification-m2",
            "compile": "uv run ratereplay-tariff compile-e1",
            "replay": "uv run ratereplay-tariff replay-e1 tariffs/examples/e1-replay-input.json",
        },
        "compilation": {
            "deterministic": True,
            "bundle_sha256": _content_sha256(compile_first),
            "compiler_content_sha256": compile_first["compiler_content_sha256"],
            "normalized_ast_sha256": reports["normalized_ast_sha256"],
            "tariff_version_id": compile_first["ir"]["tariff_version_id"],
            "operator_count": len(compile_first["ir"]["operators"]),
            "service_windows": reports["component_vector"]["service_windows"],
            "component_keys": reports["component_vector"]["complete_component_keys"],
            "active_component_count_by_key": reports["component_vector"][
                "active_component_count_by_key"
            ],
        },
        "goldens": {
            "complete_bill_sha256": _file_sha256(COMPLETE_GOLDEN),
            "boundary_suite_sha256": _file_sha256(BOUNDARY_GOLDEN),
            "complete_bill_case_id": complete_golden["case_id"],
            "boundary_case_count": len(boundary_golden["cases"]),
            "covered_rule_ids": sorted(reports["golden_coverage"]["rule_case_ids"]),
        },
        "deliberate_invalid_inputs": invalid_results,
        "rate_and_boundary_mutations": mutation_results,
        "replay": {
            "deterministic": True,
            "input_sha256": _file_sha256(INPUT),
            "result_sha256": _content_sha256(replay_first),
            "line_cents": line_cents,
            "line_total_cents": sum(line_cents),
            "supported_calculated_cents": replay_first["supported_calculated_cents"],
            "entered_bill_total_cents": reconciliation["entered_bill_total_cents"],
            "user_unsupported_cents": reconciliation["user_unsupported_cents"],
            "unexplained_residual_cents": reconciliation["unexplained_residual_cents"],
            "classification": reconciliation["classification"],
            "reconciliation_input_sha256": reconciliation["input_sha256"],
            "reconciliation_policy_sha256": reconciliation["policy_sha256"],
            "calculation_sha256": replay_first["manifest"]["calculation_sha256"],
        },
        "provenance": reports["source_coverage"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    result = qualify()
    print(
        "Milestone 2 qualification passed: "
        f"{result['replay']['supported_calculated_cents']} supported cents, "
        f"compiler {result['compilation']['compiler_content_sha256']}."
    )


if __name__ == "__main__":
    main()
