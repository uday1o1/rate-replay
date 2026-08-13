import json

from ratereplay_domain.cli import app
from typer.testing import CliRunner


def test_public_cli_converts_exact_energy() -> None:
    result = CliRunner().invoke(app, ["convert-energy", "1.250", "--unit", "kWh"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"energy_wh": 1250}


def test_public_cli_exposes_nonintegral_failure_code() -> None:
    result = CliRunner().invoke(
        app,
        ["convert-energy", "1", "--unit", "Wh", "--multiplier", "-1"],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "NON_INTEGRAL_WATT_HOUR"
