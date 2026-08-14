from __future__ import annotations

import json
from pathlib import Path

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
