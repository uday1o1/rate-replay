#!/usr/bin/env python3
"""Validate the frozen Milestone 8 evaluation run manifest."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/manifests/m8-evaluation-v1.json"


class ManifestValidationError(RuntimeError):
    """The frozen evaluation manifest is incomplete or has drifted."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ManifestValidationError(code)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _manifest_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_locked_path(entry: dict[str, Any]) -> Path:
    relative = Path(str(entry["path"]))
    _require(not relative.is_absolute() and ".." not in relative.parts, "LOCKED_PATH_UNSAFE")
    resolved = ROOT / relative
    _require(resolved.is_file(), f"LOCKED_PATH_MISSING:{relative}")
    _require(_sha256(resolved) == entry["sha256"], f"LOCKED_PATH_HASH_MISMATCH:{relative}")
    return resolved


def _locked_entries(value: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if set(value) >= {"path", "sha256"}:
            entries.append(cast(dict[str, Any], value))
        for child in value.values():
            entries.extend(_locked_entries(child))
    elif isinstance(value, list):
        for child in value:
            entries.extend(_locked_entries(child))
    return entries


def _validate_independent_runner(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)
    _require(
        not any(module.startswith("ratereplay") for module in imported_modules),
        "INDEPENDENT_RUNNER_IMPORTS_PRODUCTION",
    )


def _validate_scaling_workload(path: Path) -> None:
    workload = _json(path)
    _require(
        workload["evidence_scope"] == "SYNTHETIC_ENGINEERING_ONLY",
        "SCALING_SCOPE_OVERCLAIM",
    )
    _require("PGE_SAVINGS" in workload["prohibited_claims"], "SCALING_PROHIBITION_MISSING")
    generator = workload["generator"]
    _require(generator["interval_seconds"] == 900, "SCALING_INTERVAL_DRIFT")
    start_ns = 1_735_689_600_000_000_000
    for profile in workload["profiles"]:
        digest = hashlib.sha256(b"RateReplay.M8SyntheticIngestion.v1\0")
        for index in range(int(profile["interval_count"])):
            energy_wh = 125 + ((index * 17) % 251)
            digest.update(f"{start_ns + index * 900_000_000_000},900,{energy_wh}\n".encode("ascii"))
        _require(
            digest.hexdigest() == profile["canonical_readings_sha256"],
            f"SCALING_PROFILE_HASH_MISMATCH:{profile['profile_id']}",
        )


def validate_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = _json(path)
    _require(
        manifest["schema_version"] == "m8-evaluation-run-manifest-v1",
        "M8_MANIFEST_SCHEMA",
    )
    _require(manifest["frozen_before_execution"] is True, "M8_MANIFEST_NOT_PREFROZEN")
    _require(manifest["manifest_sha256"] == _manifest_hash(manifest), "M8_MANIFEST_HASH")
    locked = _locked_entries(manifest)
    _require(len(locked) == 29, "M8_LOCKED_INPUT_COUNT")
    resolved = {entry["path"]: _resolve_locked_path(entry) for entry in locked}

    runner_path = resolved[manifest["billing_goldens"]["independent_runner"]["path"]]
    _validate_independent_runner(runner_path)
    independent_inputs = _json(resolved[manifest["billing_goldens"]["independent_input"]["path"]])
    _require(len(independent_inputs["cases"]) == 5, "M8_GOLDEN_CASE_COUNT")
    _require(
        all(case["source_ids"] and case["source_sheets"] for case in independent_inputs["cases"]),
        "M8_GOLDEN_SOURCE_LINK_MISSING",
    )
    _require(len(manifest["billing_goldens"]["fixtures"]) == 6, "M8_GOLDEN_FIXTURE_COUNT")

    scaling_path = resolved[manifest["parser"]["scaling_workload"]["path"]]
    _validate_scaling_workload(scaling_path)
    _require(manifest["optimizer"]["load_counts"] == [0, 1, 5], "M8_LOAD_COUNTS")
    _require(manifest["optimizer"]["oracle_small_instance_count"] >= 25, "M8_ORACLE_COUNT")

    newest_charter = _json(ROOT / "benchmarks/charters/performance-v3.json")
    _require(
        manifest["performance"]["thresholds"] == newest_charter["thresholds"],
        "M8_PERFORMANCE_THRESHOLD_DRIFT",
    )
    _require(manifest["hardware"] == newest_charter["hardware"], "M8_HARDWARE_DRIFT")
    _require(manifest["performance"]["measured_repetitions"] >= 10, "M8_REPETITIONS")
    _require(manifest["performance"]["cache_series"] == ["COLD", "WARM"], "M8_CACHE_SERIES")
    _require(
        manifest["human_study"]["state"] == "HUMAN_VALIDATION_DEFERRED"
        and manifest["human_study"]["genuine_participant_count"] == 0
        and manifest["human_study"]["synthetic_sessions_may_count"] is False,
        "M8_HUMAN_STUDY_STATE",
    )
    _require(
        manifest["human_study"]["qualification_command"] == "make qualification-m6-study",
        "M8_HUMAN_STUDY_COMMAND",
    )
    _require(len(manifest["negative_cases"]) >= 9, "M8_NEGATIVE_CASE_COVERAGE")
    return manifest


def main() -> None:
    manifest = validate_manifest()
    print(
        "M8_MANIFEST_OK "
        f"manifest_sha256={manifest['manifest_sha256']} "
        f"locked_inputs={len(_locked_entries(manifest))}"
    )


if __name__ == "__main__":
    main()
