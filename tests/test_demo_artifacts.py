from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from scripts.generate_demo_artifacts import REQUIRED_LOGICAL_IDS

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "artifacts/demo"
EXAMPLE_REPORT = ROOT / "docs/results/example-redacted-report.json"


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_bytes()))


def test_public_demo_release_is_complete_content_addressed_and_redacted() -> None:
    manifest_bytes = (DEMO / "manifest.v1.json").read_bytes()
    manifest = cast(dict[str, Any], json.loads(manifest_bytes))
    lock = (ROOT / "apps/web/src/demoReleaseLock.ts").read_text(encoding="ascii")
    assert f'"{hashlib.sha256(manifest_bytes).hexdigest()}"' in lock
    assert set(manifest) == {
        "manifest_version",
        "generation_command",
        "simulated_only",
        "allowlist_sha256",
        "calculation_manifest_sha256",
        "artifacts",
    }
    assert manifest["manifest_version"] == "public-demo-manifest-v1"
    assert manifest["generation_command"] == "make demo-artifacts"
    assert manifest["simulated_only"] is True
    allowlist_path = DEMO / "allowlist.v1.json"
    allowlist_bytes = allowlist_path.read_bytes()
    assert hashlib.sha256(allowlist_bytes).hexdigest() == manifest["allowlist_sha256"]
    allowlist = _json(allowlist_path)
    assert tuple(allowlist["logical_artifact_ids"]) == REQUIRED_LOGICAL_IDS
    entries = cast(list[dict[str, str]], manifest["artifacts"])
    assert tuple(entry["logical_id"] for entry in entries) == REQUIRED_LOGICAL_IDS

    artifacts: dict[str, dict[str, Any]] = {}
    for entry in entries:
        payload = (DEMO / entry["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        assert Path(entry["path"]).stem == entry["sha256"]
        artifact = cast(dict[str, Any], json.loads(payload))
        assert artifact["schema_version"] == "public-demo-artifact-v1"
        assert artifact["logical_id"] == entry["logical_id"]
        assert artifact["simulated"] is True
        artifacts[entry["logical_id"]] = artifact

    comparison = cast(dict[str, Any], artifacts["tariff-comparison"]["payload"])
    assert comparison["rankable"] is True
    assert comparison["winner_tariff_version_ids"] == ["pge-etoud-2026-07"]
    solver = cast(dict[str, Any], artifacts["solver-result"]["payload"])
    assert solver["search_status"] == "OPTIMAL"
    verification = cast(dict[str, Any], artifacts["verification-record"]["payload"])
    assert verification["status"] == "VALID"

    report = cast(dict[str, Any], artifacts["redacted-report"]["payload"])
    assert _json(EXAMPLE_REPORT) == report
    assert set(report) == {
        "schema_version",
        "redaction_policy_version",
        "report_template_version",
        "calculation_time_mode",
        "historical_addition_label",
        "billing_period",
        "aggregate_measured_energy_wh",
        "aggregate_reference_flexible_energy_wh",
        "aggregate_shifted_energy_wh",
        "selected_supported_cost_cents",
        "reference_supported_cost_cents",
        "supported_cost_difference_cents",
        "signed_unexplained_residual_cents",
        "supported_charge_components",
        "unsupported_component_codes",
        "tariff_provenance",
        "solver",
        "scenario_result_version",
        "scenario_result_sha256",
        "limitations",
        "report_sha256",
    }
    serialized = json.dumps(report, sort_keys=True)
    for prohibited in (
        "slot_start_utc",
        "occurrence_id",
        "physical_asset_key",
        "reference_schedule",
        "source_id",
        "object_key",
        "Simulated current-bill local tax",
    ):
        assert prohibited not in serialized
