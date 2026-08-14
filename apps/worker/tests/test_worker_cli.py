import pytest
from ratereplay_worker.cli import app
from typer.testing import CliRunner


def test_worker_cli_requires_database_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATEREPLAY_DATABASE_URL", raising=False)
    result = CliRunner().invoke(app, ["run-once"])
    assert result.exit_code == 2
    assert "RATEREPLAY_DATABASE_URL is required" in result.output


def test_worker_cli_exposes_one_shot_and_continuous_modes() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run-once" in result.output
    assert "run" in result.output
