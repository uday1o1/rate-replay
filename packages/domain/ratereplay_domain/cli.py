"""Foundation diagnostics exposed through the public development CLI."""

from __future__ import annotations

import json

import typer

from ratereplay_domain.energy import EnergyAdmissionError, exact_watt_hours

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Exercise frozen foundation contracts through the public CLI."""


@app.command("convert-energy")
def convert_energy(value: str, unit: str = "Wh", multiplier: int = 0) -> None:
    """Exercise the exact public energy-admission path."""

    try:
        result = {
            "energy_wh": exact_watt_hours(
                value, source_unit=unit, power_of_ten_multiplier=multiplier
            )
        }
    except EnergyAdmissionError as error:
        typer.echo(json.dumps({"code": error.code, "message": str(error)}, sort_keys=True))
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(result, sort_keys=True))
