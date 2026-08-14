"""Content-addressed production tariff-admission lock validation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import Field

from ratereplay_tariffs.compiled import CompilationBundle
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff
from ratereplay_tariffs.schema import FrozenModel


class LockedArtifact(FrozenModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdmissionScope(FrozenModel):
    service_provider: Literal["PG&E"]
    service_mode: Literal["BUNDLED"]
    income_tier: Literal["TIER_3"]
    baseline_territory: Literal["T"]
    baseline_quantity_code: Literal["BASIC"]
    calculation_time_mode: Literal["HISTORICAL_REPLAY"]
    comparison_admitted: Literal[True]
    optimization_admitted: Literal[False]


class AdmissionGateEvidence(FrozenModel):
    component_vector_complete: Literal[True]
    eligibility_fail_closed: Literal[True]
    golden_coverage_complete: Literal[True]
    integer_bounds_proved: Literal[True]
    mutation_suite_passed: Literal[True]
    source_links_retrievable: Literal[True]
    stable_compiler_hash: Literal[True]


class TariffAdmissionLock(FrozenModel):
    admission_lock_version: Literal["tariff-admission-lock-v1"]
    tariff_version_id: str
    plan_code: str
    admission_status: Literal["ADMITTED"]
    admitted_at: str
    admitted_service_windows: tuple[tuple[str, str], ...]
    target_account_predicate_id: str
    definition: LockedArtifact
    compiler_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ids: tuple[str, ...]
    source_hashes: tuple[str, ...]
    golden_suites: tuple[LockedArtifact, ...]
    scope: AdmissionScope
    gate_evidence: AdmissionGateEvidence


class AdmittedTariff(FrozenModel):
    lock: TariffAdmissionLock
    compilation: CompilationBundle


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locked_path(root: Path, artifact: LockedArtifact) -> Path:
    path = (root / artifact.path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise TariffCompileError(
            "ADMISSION_PATH_ESCAPE", "Admission artifact escapes repository root"
        ) from error
    if _sha256(path) != artifact.sha256:
        raise TariffCompileError(
            "ADMISSION_ARTIFACT_MISMATCH", f"Admission artifact differs: {artifact.path}"
        )
    return path


_LOCK_PATHS = {
    "E-1": "tariffs/admission/pge-e1-2026-07.json",
    "E-TOU-C": "tariffs/admission/pge-etouc-2026-07.json",
    "E-TOU-D": "tariffs/admission/pge-etoud-2026-07.json",
    "E-ELEC": "tariffs/admission/pge-eelec-2026-07.json",
    "EV2-A": "tariffs/admission/pge-ev2a-2026-07.json",
}


def load_admitted_tariff(root: Path, plan_code: str) -> AdmittedTariff:
    relative_lock_path = _LOCK_PATHS.get(plan_code)
    if relative_lock_path is None:
        raise TariffCompileError("TARIFF_NOT_ADMITTED", f"Tariff {plan_code} is not admitted")
    lock_path = root / relative_lock_path
    try:
        lock = TariffAdmissionLock.model_validate_json(lock_path.read_bytes())
    except (OSError, ValueError) as error:
        raise TariffCompileError(
            "ADMISSION_LOCK_INVALID", f"{plan_code} admission lock is invalid"
        ) from error
    if lock.plan_code != plan_code:
        raise TariffCompileError("ADMISSION_VERSION_MISMATCH", "Admission plan code differs")
    definition_path = _locked_path(root, lock.definition)
    for golden in lock.golden_suites:
        _locked_path(root, golden)
    compilation = compile_tariff(root, definition_path)
    if compilation.compiler_content_sha256 != lock.compiler_content_sha256:
        raise TariffCompileError("ADMISSION_COMPILER_MISMATCH", "Compiler output differs from lock")
    normalized = compilation.normalized_ast
    if normalized.get("tariff_version_id") != lock.tariff_version_id:
        raise TariffCompileError("ADMISSION_VERSION_MISMATCH", "Tariff version differs from lock")
    source_ids = normalized.get("source_ids")
    if not isinstance(source_ids, list) or tuple(source_ids) != lock.source_ids:
        raise TariffCompileError("ADMISSION_SOURCE_MISMATCH", "Tariff sources differ from lock")
    source_hashes = normalized.get("source_hashes")
    if not isinstance(source_hashes, list) or tuple(source_hashes) != lock.source_hashes:
        raise TariffCompileError("ADMISSION_SOURCE_MISMATCH", "Tariff source hashes differ")
    if compilation.reports.eligibility_predicate_id != lock.target_account_predicate_id:
        raise TariffCompileError(
            "ADMISSION_ELIGIBILITY_MISMATCH", "Eligibility predicate differs from lock"
        )
    windows = tuple(
        (window.start.isoformat(), window.end.isoformat() if window.end else "")
        for window in compilation.reports.component_vector.service_windows
    )
    if windows != lock.admitted_service_windows:
        raise TariffCompileError("ADMISSION_WINDOW_MISMATCH", "Service windows differ from lock")
    return AdmittedTariff(lock=lock, compilation=compilation)


def load_admitted_e1(root: Path) -> AdmittedTariff:
    return load_admitted_tariff(root, "E-1")


def load_all_admitted_tariffs(root: Path) -> tuple[AdmittedTariff, ...]:
    return tuple(load_admitted_tariff(root, plan_code) for plan_code in _LOCK_PATHS)
