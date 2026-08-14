from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from scripts.qualify_m8_correctness import (
    COMPARISON_OUTPUT,
    GOLDEN_OUTPUT,
    OPTIMIZER_OUTPUT,
    PARSER_OUTPUT,
    _artifact_hash,
)


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def test_m8_correctness_evidence_is_complete_and_self_hashed() -> None:
    for path in (GOLDEN_OUTPUT, PARSER_OUTPUT, COMPARISON_OUTPUT, OPTIMIZER_OUTPUT):
        payload = _json(path)
        assert payload["gate_result"] == "PASS"
        assert payload["artifact_sha256"] == _artifact_hash(payload)


def test_m8_oracle_covers_every_advertised_tariff() -> None:
    payload = _json(OPTIMIZER_OUTPUT)
    cases = cast(list[dict[str, Any]], payload["cases"])
    assert len(cases) == 25
    assert {case["plan_code"] for case in cases} == {
        "E-1",
        "E-TOU-C",
        "E-TOU-D",
        "E-ELEC",
        "EV2-A",
    }
    assert all(case["passed"] for case in cases)


def test_m8_comparison_has_complete_component_coverage() -> None:
    payload = _json(COMPARISON_OUTPUT)
    candidates = cast(list[dict[str, Any]], payload["candidates"])
    assert payload["rankable"] is True
    assert payload["candidate_count"] == 5
    assert all(candidate["complete"] for candidate in candidates)
