from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "docs/architecture.md",
    ROOT / "docs/billing-semantics.md",
    ROOT / "docs/tariff-authoring.md",
    ROOT / "docs/validation-methodology.md",
    ROOT / "docs/security-and-privacy.md",
    ROOT / "docs/limitations.md",
    ROOT / "docs/user-study-handoff.md",
)
LINK_PATTERN = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def _load(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def test_required_public_documents_are_present_formatted_and_locally_linked() -> None:
    for document in PUBLIC_DOCUMENTS:
        content = document.read_text(encoding="utf-8")
        assert content.startswith("# ")
        assert "—" not in content
        for link in LINK_PATTERN.findall(content):
            target_text = link.split("#", 1)[0]
            if not target_text or "://" in target_text:
                continue
            target = (document.parent / target_text).resolve()
            target.relative_to(ROOT)
            assert target.exists(), f"Broken local link in {document.relative_to(ROOT)}: {link}"


def test_readme_status_demo_and_measurements_match_qualified_evidence() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    summary = _load(ROOT / "evidence/evaluation/m8-summary.json")
    performance = _load(ROOT / "evidence/evaluation/m8-performance.json")
    rows = {row["metric_id"]: row for row in performance["measurements"]}

    assert "under active implementation" not in readme
    assert "public demo remains unavailable" not in readme
    assert "HUMAN_VALIDATION_DEFERRED" in readme
    assert summary["human_validation"]["state"] == "HUMAN_VALIDATION_DEFERRED"
    assert summary["human_validation"]["genuine_participant_count"] == 0
    assert "http://127.0.0.1:4173/#demo" in readme
    assert "docs/results/example-redacted-report.json" in readme

    expected = {
        "variance-followup:import:one-year-15-minute:warm": "427.883 ms",
        "optimization:scale-5:warm": "2,568.952 ms",
        "release-api:warm_cached_comparison_get": "64.432 ms",
        "release-api:warm_scenario_get": "333.020 ms",
        "release-crash:import_worker_recovery": "23,589.761 ms",
        "release-crash:scenario_worker_recovery": "27,277.055 ms",
    }
    for metric_id, displayed in expected.items():
        assert rows[metric_id]["passed"] is True
        assert f"{float(rows[metric_id]['p95_ms']):,.3f} ms" == displayed
        assert displayed in readme


def test_handoff_preserves_genuine_and_synthetic_evidence_boundary() -> None:
    handoff = (ROOT / "docs/user-study-handoff.md").read_text(encoding="utf-8")

    assert "make qualification-m6-study" in handoff
    assert "exactly five first-time RateReplay users" in handoff
    assert "counts as zero participants" in handoff
    assert "make qualification-m7-restore" in handoff
    assert "make qualification-m7-deployment" in handoff
    assert "make finalize-m8-evaluation" in handoff
    assert "make qualification-m8" in handoff
    assert "make m9-clean-container-check" in handoff
