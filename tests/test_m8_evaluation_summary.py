from __future__ import annotations

import csv
import io
import json
from typing import Any, cast

from scripts.finalize_m8_evaluation import (
    CSV_PATH,
    PERFORMANCE_PATH,
    SUMMARY_PATH,
    SVG_PATH,
    _source_evidence,
    build_outputs,
    build_rows,
)


def test_m8_evaluation_views_match_qualified_evidence() -> None:
    outputs = build_outputs()
    assert outputs[SUMMARY_PATH] == SUMMARY_PATH.read_text(encoding="utf-8")
    assert outputs[PERFORMANCE_PATH] == PERFORMANCE_PATH.read_text(encoding="utf-8")
    assert outputs[CSV_PATH] == CSV_PATH.read_text(encoding="utf-8")
    assert outputs[SVG_PATH] == SVG_PATH.read_text(encoding="utf-8")


def test_m8_summary_does_not_accept_deferred_human_gate() -> None:
    summary = cast(dict[str, Any], json.loads(build_outputs()[SUMMARY_PATH]))
    assert summary["implementation_status"] == "IMPLEMENTED_PENDING_GATE"
    assert summary["acceptance_gate_result"] == "DEFERRED"
    assert summary["human_validation"] == {
        "after_human_qualification_command": "make qualification-m8",
        "genuine_participant_count": 0,
        "qualification_command": "make qualification-m6-study",
        "state": "HUMAN_VALIDATION_DEFERRED",
        "synthetic_personas_are_development_only": True,
        "synthetic_sessions_counted": 0,
    }
    assert summary["public_claim_boundary"]["genuine_human_comprehension_claim"] is False


def test_m8_performance_aggregate_preserves_source_measurements() -> None:
    performance = cast(dict[str, Any], json.loads(build_outputs()[PERFORMANCE_PATH]))
    assert performance["gate_result"] == "PASS"
    assert performance["measurement_count"] == 25
    assert performance["original_high_variance_measurements_preserved"] is True
    assert performance["duplicate_successful_results"] == 0


def test_m8_csv_contains_all_measurements_without_merging_cache_states() -> None:
    expected_rows = build_rows(_source_evidence())
    actual_rows = list(csv.DictReader(io.StringIO(build_outputs()[CSV_PATH])))
    assert len(actual_rows) == len(expected_rows) == 25
    assert {row["cache_state"] for row in actual_rows} >= {"COLD", "WARM"}
    assert {row["environment"] for row in actual_rows} == {
        "LOCAL_CORE",
        "LOCAL_CORE_VARIANCE_FOLLOWUP",
        "LOCAL_RELEASE_TOPOLOGY",
    }


def test_m8_svg_is_accessible_and_discloses_deferred_human_gate() -> None:
    svg = build_outputs()[SVG_PATH]
    assert 'role="img"' in svg
    assert "<title" in svg
    assert "<desc" in svg
    assert "genuine five-person comprehension gate is deferred" in svg
