#!/usr/bin/env python3
"""Derive complete-bill golden totals without importing production code."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "benchmarks/reference/m8-golden-inputs.v1.json"
DEFAULT_OUTPUT = ROOT / "evidence/correctness/m8-independent-golden-derivations.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _self_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _round_ratio(numerator: int, denominator: int) -> int:
    value = Decimal(numerator) / Decimal(denominator)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _derive_line(line: dict[str, Any]) -> dict[str, Any]:
    kind = line["kind"]
    if kind == "ENERGY":
        numerator = int(line["energy_wh"]) * int(line["rate_microdollars_per_kwh"])
        denominator = 10_000_000
    elif kind == "DAILY":
        numerator = int(line["days"]) * int(line["rate_microdollars_per_day"])
        denominator = 10_000
    elif kind == "FIXED":
        numerator = int(line["amount_microdollars"])
        denominator = 10_000
    else:
        raise ValueError(f"unsupported independent line kind: {kind}")
    return {
        "kind": kind,
        "input": line,
        "pre_round_numerator": numerator,
        "pre_round_denominator": denominator,
        "derived_cents": _round_ratio(numerator, denominator),
    }


def derive(input_path: Path = DEFAULT_INPUT) -> dict[str, Any]:
    inputs = cast(dict[str, Any], json.loads(input_path.read_text(encoding="utf-8")))
    results: list[dict[str, Any]] = []
    for case in inputs["cases"]:
        lines = [_derive_line(line) for line in case["lines"]]
        derived_line_cents = [line["derived_cents"] for line in lines]
        derived_total_cents = sum(derived_line_cents)
        golden_path = ROOT / case["golden_path"]
        results.append(
            {
                "case_id": case["case_id"],
                "golden_path": case["golden_path"],
                "golden_sha256": _sha256(golden_path),
                "source_ids": case["source_ids"],
                "source_sheets": case["source_sheets"],
                "rule_ids": case["rule_ids"],
                "lines": lines,
                "derived_line_cents": derived_line_cents,
                "expected_line_cents": case["expected_line_cents"],
                "derived_total_cents": derived_total_cents,
                "expected_total_cents": case["expected_total_cents"],
                "passed": derived_line_cents == case["expected_line_cents"]
                and derived_total_cents == case["expected_total_cents"],
            }
        )
    payload: dict[str, Any] = {
        "schema_version": "m8-independent-golden-derivations-v1",
        "evidence_class": "INDEPENDENT_STANDARD_LIBRARY_ARITHMETIC",
        "production_imports": [],
        "input_path": str(input_path.relative_to(ROOT)),
        "input_sha256": _sha256(input_path),
        "case_count": len(results),
        "cases": results,
        "gate_result": "PASS" if all(result["passed"] for result in results) else "FAIL",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    payload = derive(arguments.input.resolve())
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    output = arguments.output.resolve()
    if arguments.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("M8_INDEPENDENT_GOLDEN_DERIVATION_DRIFT")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    if payload["gate_result"] != "PASS":
        raise SystemExit("M8_INDEPENDENT_GOLDEN_DERIVATION_FAILED")
    print(
        "M8_INDEPENDENT_GOLDENS_PASS "
        f"cases={payload['case_count']} artifact_sha256={payload['artifact_sha256']}"
    )


if __name__ == "__main__":
    main()
