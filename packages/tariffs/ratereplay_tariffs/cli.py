"""Public tariff compilation and historical replay CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ratereplay_tariffs.admission import load_all_admitted_tariffs
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayError,
    ReplayRequest,
    replay_compiled_tariff,
)
from ratereplay_tariffs.comparison import (
    ComparisonError,
    compare_admitted_tariffs,
    load_required_component_keys,
)
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff

app = typer.Typer(no_args_is_help=True)
DEFAULT_ROOT = Path(".")


def _emit(payload: str, output: Path | None) -> None:
    if output is None:
        typer.echo(payload)
    else:
        output.write_text(payload + "\n", encoding="utf-8")


def _fail(code: str, message: str) -> None:
    typer.echo(json.dumps({"code": code, "message": message}, sort_keys=True))
    raise typer.Exit(code=2)


@app.command("compile-e1")
def compile_e1(
    root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = DEFAULT_ROOT,
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Compile the locked July 2026 E-1 definition and print every report."""

    try:
        bundle = compile_tariff(root.resolve())
    except TariffCompileError as error:
        _fail(error.code, str(error))
    _emit(bundle.model_dump_json(indent=2), output)


@app.command("compile")
def compile_definition(
    definition_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = DEFAULT_ROOT,
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Compile one source-locked declarative tariff definition."""

    try:
        bundle = compile_tariff(root.resolve(), definition_path.resolve())
    except TariffCompileError as error:
        _fail(error.code, str(error))
    _emit(bundle.model_dump_json(indent=2), output)


@app.command("replay-e1")
def replay_e1(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = DEFAULT_ROOT,
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Replay one locked July 2026 E-1 billing period from a strict JSON request."""

    try:
        request = ReplayRequest.model_validate_json(input_path.read_bytes())
        result = replay_compiled_tariff(compile_tariff(root.resolve()), request)
    except TariffCompileError as error:
        _fail(error.code, str(error))
    except ReplayError as error:
        _fail(error.code, str(error))
    except ValidationError as error:
        _fail("REPLAY_REQUEST_INVALID", str(error))
    except OSError as error:
        _fail("REPLAY_INPUT_UNREADABLE", str(error))
    _emit(result.model_dump_json(indent=2), output)


@app.command("replay")
def replay_definition(
    definition_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = DEFAULT_ROOT,
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Replay one source-locked tariff from a strict aggregate or interval request."""

    try:
        raw = json.loads(input_path.read_bytes())
        if not isinstance(raw, dict):
            _fail("REPLAY_REQUEST_INVALID", "Replay request must be a JSON object")
        request: ReplayRequest | IntervalReplayRequest
        if raw.get("request_version") == "interval-replay-request-v1":
            request = IntervalReplayRequest.model_validate_json(input_path.read_bytes())
        else:
            request = ReplayRequest.model_validate_json(input_path.read_bytes())
        result = replay_compiled_tariff(
            compile_tariff(root.resolve(), definition_path.resolve()), request
        )
    except TariffCompileError as error:
        _fail(error.code, str(error))
    except ReplayError as error:
        _fail(error.code, str(error))
    except (ValidationError, json.JSONDecodeError) as error:
        _fail("REPLAY_REQUEST_INVALID", str(error))
    except OSError as error:
        _fail("REPLAY_INPUT_UNREADABLE", str(error))
    _emit(result.model_dump_json(indent=2), output)


@app.command("compare")
def compare(
    input_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = DEFAULT_ROOT,
    current_tariff_version_id: Annotated[
        str, typer.Option(help="Current replay tariff version in the candidate set")
    ] = "pge-e1-2026-07",
    output: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
) -> None:
    """Compare a strict interval request across every admitted tariff."""

    try:
        resolved_root = root.resolve()
        request = IntervalReplayRequest.model_validate_json(input_path.read_bytes())
        result = compare_admitted_tariffs(
            load_all_admitted_tariffs(resolved_root),
            request,
            current_tariff_version_id=current_tariff_version_id,
            required_component_keys=load_required_component_keys(resolved_root),
        )
    except TariffCompileError as error:
        _fail(error.code, str(error))
    except ComparisonError as error:
        _fail(error.code, str(error))
    except ValidationError as error:
        _fail("COMPARISON_REQUEST_INVALID", str(error))
    except OSError as error:
        _fail("COMPARISON_INPUT_UNREADABLE", str(error))
    _emit(result.model_dump_json(indent=2), output)


if __name__ == "__main__":
    app()
