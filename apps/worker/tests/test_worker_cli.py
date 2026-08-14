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
    assert "reconcile-deletions-once" in result.output
    assert "reconcile-deletions" in result.output


def test_deletion_reconciler_requires_separate_control_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATEREPLAY_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.delenv("RATEREPLAY_DELETION_LEDGER_KEY_FILE", raising=False)
    monkeypatch.delenv("RATEREPLAY_RESTORE_KEY_FILE", raising=False)
    result = CliRunner().invoke(app, ["reconcile-deletions-once"])
    assert result.exit_code == 2
    assert "RATEREPLAY_DELETION_LEDGER_KEY_FILE is required" in result.output
