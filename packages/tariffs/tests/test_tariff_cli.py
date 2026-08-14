from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ratereplay_tariffs.cli import app
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


def test_public_cli_compiles_full_bundle() -> None:
    result = CliRunner().invoke(app, ["compile-e1", "--root", str(ROOT)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["compiler_content_sha256"] == (
        "ae003e7717fbb8fa964aac75ba21efa737f4db54bdba2abcb90b1a22d81a0016"
    )
    assert payload["reports"]["component_vector"]["active_component_count_by_key"] == [1, 1]


def test_public_generic_cli_compiles_etouc_bundle() -> None:
    result = CliRunner().invoke(
        app,
        [
            "compile",
            str(ROOT / "tariffs/definitions/pge-etouc-2026-07.json"),
            "--root",
            str(ROOT),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ir"]["tariff_version_id"] == "pge-etouc-2026-07"
    assert any(
        operator["operator"] == "TIME_OF_USE_MULTIPLY_WITH_OPTIONAL_BASELINE_CREDIT"
        for operator in payload["ir"]["operators"]
    )


def test_public_cli_replays_example_with_visible_residual() -> None:
    result = CliRunner().invoke(
        app,
        [
            "replay-e1",
            str(ROOT / "tariffs/examples/e1-replay-input.json"),
            "--root",
            str(ROOT),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["supported_calculated_cents"] == 9819
    assert payload["reconciliation"]["user_unsupported_cents"] == 200
    assert payload["reconciliation"]["unexplained_residual_cents"] == 981
    assert len(payload["provenance_sources"]) == 2


def test_public_cli_rejects_invalid_request(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(app, ["replay-e1", str(path), "--root", str(ROOT)])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "REPLAY_REQUEST_INVALID"


def test_public_cli_compares_all_admitted_tariffs(tmp_path: Path) -> None:
    profile = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "data/demo/july-2026-simulated-profile.json").read_text(encoding="utf-8")
        ),
    )
    account = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
        ),
    )
    request = {
        "request_version": "interval-replay-request-v1",
        "profile_content_sha256": (
            "47b449f47039960cde24666a5ed2723781b7773d624dbdd2b74de78e02da19ce"
        ),
        "account_facts": account["account_facts"],
        "energy_wh": profile["total_energy_wh"],
        "intervals": [
            {
                "start_utc_ns": int(
                    datetime.fromisoformat(
                        cast(str, reading["start_utc"]).replace("Z", "+00:00")
                    ).timestamp()
                )
                * 1_000_000_000,
                "duration_seconds": reading["duration_seconds"],
                "energy_wh": reading["energy_wh"],
            }
            for reading in cast(list[dict[str, Any]], profile["readings"])
        ],
        "dated_eligibility_facts": account["dated_eligibility_facts"],
    }
    input_path = tmp_path / "comparison.json"
    input_path.write_text(json.dumps(request), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "compare",
            str(input_path),
            "--root",
            str(ROOT),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["rankable"] is True
    assert payload["winner_tariff_version_ids"] == ["pge-etoud-2026-07"]
    assert payload["savings_against_current_supported_cents"] == 1707
    assert len(payload["candidates"]) == 5
