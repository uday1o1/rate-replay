from __future__ import annotations

import json
from typing import Any, cast

from benchmarks.scripts.m8_performance import (
    CORE_OUTPUT,
    VARIANCE_OUTPUT,
    validate_core,
    validate_variance_followup,
)


def test_m8_performance_core_evidence_passes_frozen_thresholds() -> None:
    payload = cast(dict[str, Any], json.loads(CORE_OUTPUT.read_text(encoding="utf-8")))
    validate_core(payload)
    assert payload["gate_result"] == "PASS"
    assert all(item["repetitions"] == 10 for item in payload["import_measurements"])
    assert all(item["repetitions"] == 10 for item in payload["operation_measurements"])


def test_m8_performance_keeps_cold_and_warm_results_separate() -> None:
    payload = cast(dict[str, Any], json.loads(CORE_OUTPUT.read_text(encoding="utf-8")))
    measurements = [*payload["import_measurements"], *payload["operation_measurements"]]
    assert {item["cache_state"] for item in measurements} == {"COLD", "WARM"}
    assert all(len(item["durations_ms"]) == item["repetitions"] for item in measurements)


def test_m8_performance_preserves_and_investigates_high_variance() -> None:
    original = cast(dict[str, Any], json.loads(CORE_OUTPUT.read_text(encoding="utf-8")))
    followup = cast(dict[str, Any], json.loads(VARIANCE_OUTPUT.read_text(encoding="utf-8")))
    validate_variance_followup(followup)
    assert followup["original_artifact_sha256"] == original["artifact_sha256"]
    assert followup["original_results_preserved"] is True
    assert followup["gate_result"] == "PASS"
