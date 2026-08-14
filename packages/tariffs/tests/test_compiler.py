from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ratereplay_tariffs.admission import load_admitted_e1
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff

ROOT = Path(__file__).resolve().parents[3]
DEFINITION = ROOT / "tariffs/definitions/pge-e1-2026-07.json"


def _mutated_definition(tmp_path: Path, mutation: Callable[[dict[str, Any]], None]) -> Path:
    payload: dict[str, Any] = json.loads(DEFINITION.read_text(encoding="utf-8"))
    mutation(payload)
    path = tmp_path / "tariff.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _compile_error(path: Path, code: str) -> None:
    with pytest.raises(TariffCompileError) as raised:
        compile_tariff(ROOT, path)
    assert raised.value.code == code


def test_e1_compilation_is_deterministic_and_complete() -> None:
    first = compile_tariff(ROOT)
    second = compile_tariff(ROOT)

    assert first == second
    assert first.compiler_content_sha256 == (
        "ae003e7717fbb8fa964aac75ba21efa737f4db54bdba2abcb90b1a22d81a0016"
    )
    assert first.ir.tariff_version_id == "pge-e1-2026-07"
    assert len(first.ir.operators) == 4
    assert first.reports.component_vector.active_component_count_by_key == (1, 1)
    assert set(first.reports.golden_coverage.rule_case_ids) >= {
        "E1_APPLICABILITY_2026_03_01",
        "E1_BASELINE_T_BASIC_SUMMER_2026_03_01",
        "E1_TOTAL_ENERGY_2026_06_01",
        "BSC_TIER3_2026_06_01",
        "CALIFORNIA_CLIMATE_CREDIT_2026",
    }


def test_compiled_models_are_immutable() -> None:
    compiled = compile_tariff(ROOT)
    with pytest.raises(ValidationError):
        compiled.ir.maximum_energy_wh = 1


def test_component_coverage_gap_fails(tmp_path: Path) -> None:
    def create_gap(payload: dict[str, Any]) -> None:
        payload["component_versions"][0]["effective_range"]["start"] = "2026-07-02"

    _compile_error(_mutated_definition(tmp_path, create_gap), "COMPONENT_COVERAGE_GAP")


def test_component_overlap_fails(tmp_path: Path) -> None:
    def create_overlap(payload: dict[str, Any]) -> None:
        duplicate = dict(payload["component_versions"][0])
        duplicate["component_version_id"] = "duplicate-component"
        payload["component_versions"].append(duplicate)

    _compile_error(_mutated_definition(tmp_path, create_overlap), "COMPONENT_OVERLAP")


def test_rule_date_gap_fails(tmp_path: Path) -> None:
    def create_gap(payload: dict[str, Any]) -> None:
        payload["charge_rules"][2]["effective_range"]["start"] = "2026-07-02"

    _compile_error(_mutated_definition(tmp_path, create_gap), "RULE_COVERAGE_GAP")


def test_invalid_tier_fails(tmp_path: Path) -> None:
    def invalidate_tier(payload: dict[str, Any]) -> None:
        payload["charge_rules"][1]["tiers"][-1]["upper_bound_kind"] = "BASELINE_ALLOWANCE"

    _compile_error(_mutated_definition(tmp_path, invalidate_tier), "INVALID_TIER")


def test_unknown_unit_fails(tmp_path: Path) -> None:
    def replace_unit(payload: dict[str, Any]) -> None:
        payload["charge_rules"][1]["rate_unit"] = "cents/therm"

    _compile_error(_mutated_definition(tmp_path, replace_unit), "UNKNOWN_UNIT")


def test_source_hash_mismatch_fails(tmp_path: Path) -> None:
    def replace_hash(payload: dict[str, Any]) -> None:
        payload["component_versions"][0]["source"]["source_sha256"] = "0" * 64

    _compile_error(_mutated_definition(tmp_path, replace_hash), "SOURCE_HASH_MISMATCH")


def test_rule_component_source_mismatch_fails(tmp_path: Path) -> None:
    def replace_source(payload: dict[str, Any]) -> None:
        payload["charge_rules"][0]["source"] = payload["component_versions"][1]["source"]

    _compile_error(_mutated_definition(tmp_path, replace_source), "RULE_COMPONENT_SOURCE_MISMATCH")


def test_int64_overflow_fails(tmp_path: Path) -> None:
    def overflow_rate(payload: dict[str, Any]) -> None:
        payload["charge_rules"][1]["tiers"][0]["rate_microdollars_per_kwh"] = 2**63

    _compile_error(_mutated_definition(tmp_path, overflow_rate), "INT64_OVERFLOW")


def test_extra_definition_field_fails_closed(tmp_path: Path) -> None:
    def add_field(payload: dict[str, Any]) -> None:
        payload["charge_rules"][0]["silently_ignored"] = True

    _compile_error(_mutated_definition(tmp_path, add_field), "SCHEMA_INVALID")


def test_admission_lock_reproduces_exact_compiler_bundle() -> None:
    admitted = load_admitted_e1(ROOT)
    assert admitted.lock.admission_status == "ADMITTED"
    assert admitted.lock.compiler_content_sha256 == admitted.compilation.compiler_content_sha256


def test_admission_lock_detects_artifact_mutation(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "tariffs", tmp_path / "tariffs")
    golden = tmp_path / "tariffs/golden/e1-july-2026-boundaries.json"
    golden.write_text(golden.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(TariffCompileError) as raised:
        load_admitted_e1(tmp_path)
    assert raised.value.code == "ADMISSION_ARTIFACT_MISMATCH"
