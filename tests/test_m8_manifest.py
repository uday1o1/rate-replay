from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.validate_m8_manifest import (
    DEFAULT_MANIFEST,
    ManifestValidationError,
    _manifest_hash,
    validate_manifest,
)


def test_m8_manifest_locks_every_final_evaluation_input_before_execution() -> None:
    manifest = validate_manifest()

    assert manifest["manifest_sha256"] == _manifest_hash(manifest)
    assert manifest["frozen_before_execution"] is True
    assert manifest["human_study"]["state"] == "HUMAN_VALIDATION_DEFERRED"


def test_independent_golden_runner_has_no_production_import() -> None:
    source = Path("benchmarks/reference/m8_golden_derivations.py").read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(module.startswith("ratereplay") for module in imported)


def test_manifest_hash_mutation_fails_closed(tmp_path: Path) -> None:
    mutated = DEFAULT_MANIFEST.read_text(encoding="utf-8").replace(
        '"measured_repetitions": 10',
        '"measured_repetitions": 9',
    )
    path = tmp_path / "mutated.json"
    path.write_text(mutated, encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="M8_MANIFEST_HASH"):
        validate_manifest(path)
