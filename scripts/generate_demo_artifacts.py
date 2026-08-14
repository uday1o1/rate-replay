#!/usr/bin/env python3
"""Generate the immutable, content-addressed public demo release."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from ratereplay_ingestion.simulated import load_locked_simulated_profile
from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ReferenceSlot,
    ScenarioElectricalConstraints,
    ScenarioInput,
)
from ratereplay_optimizer.results import ScenarioOptimizationResult, build_scenario_result
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import (
    default_solver_configuration,
    optimize_exact,
    optimize_off_peak_heuristic,
)
from ratereplay_reports.redacted import build_redacted_report
from ratereplay_tariffs.admission import AdmittedTariff, load_all_admitted_tariffs
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    ReplayRequest,
    UserUnsupportedLine,
    replay_compiled_tariff,
)
from ratereplay_tariffs.comparison import (
    compare_admitted_tariffs,
    load_required_component_keys,
)
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts/demo"
TYPESCRIPT_LOCK = ROOT / "apps/web/src/demoReleaseLock.ts"
ACCOUNT = ROOT / "tariffs/examples/m3-comparison-account.json"
WORKLOAD = ROOT / "benchmarks/workloads/m4-july-optimization-v2.json"
REQUIRED_LOGICAL_IDS = (
    "import-review",
    "bill-replay",
    "tariff-comparison",
    "scenario-inputs",
    "reference-result",
    "heuristic-result",
    "solver-result",
    "verification-record",
    "redacted-report",
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RuntimeError(f"DEMO_INPUT_NOT_OBJECT:{path.name}")
    return cast(dict[str, Any], value)


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _total_energy_wh() -> int:
    return sum(
        reading.energy_wh for reading in load_locked_simulated_profile(ROOT).content.readings
    )


def _facts() -> tuple[AccountFacts, DatedEligibilityFacts]:
    payload = _json(ACCOUNT)
    return (
        AccountFacts.model_validate_json(json.dumps(payload["account_facts"])),
        DatedEligibilityFacts.model_validate_json(json.dumps(payload["dated_eligibility_facts"])),
    )


def _interval_request(
    account: AccountFacts,
    dated: DatedEligibilityFacts,
) -> IntervalReplayRequest:
    locked = load_locked_simulated_profile(ROOT)
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256=locked.content.sha256(),
        account_facts=account,
        energy_wh=_total_energy_wh(),
        intervals=tuple(
            ReplayInterval(
                start_utc_ns=reading.start_utc_ns,
                duration_seconds=reading.duration_seconds,
                energy_wh=reading.energy_wh,
            )
            for reading in locked.content.readings
        ),
        dated_eligibility_facts=dated,
    )


def _scenario(
    account: AccountFacts,
    dated: DatedEligibilityFacts,
    tariffs: tuple[AdmittedTariff, ...],
) -> ScenarioOptimizationResult:
    locked = load_locked_simulated_profile(ROOT)
    workload = _json(WORKLOAD)
    template = cast(dict[str, Any], workload["load_template"])
    occurrence = cast(dict[str, Any], template["occurrence"])
    positive = {
        datetime.fromisoformat(cast(str, item[0]).replace("Z", "+00:00")): cast(int, item[1])
        for item in cast(list[list[object]], occurrence["positive_reference_slots"])
    }
    slots = tuple(
        CanonicalProfileSlot(
            slot_start_utc=datetime.fromtimestamp(
                reading.start_utc_ns / 1_000_000_000,
                tz=UTC,
            ),
            duration_seconds=reading.duration_seconds,
            measured_energy_wh=reading.energy_wh,
        )
        for reading in locked.content.readings
    )
    reference = tuple(
        ReferenceSlot(
            slot_start_utc=slot.slot_start_utc,
            duration_seconds=slot.duration_seconds,
            energy_wh=positive.get(slot.slot_start_utc, 0),
        )
        for slot in slots
    )
    required_energy_wh = cast(int, occurrence["required_energy_wh"])
    if sum(slot.energy_wh for slot in reference) != required_energy_wh:
        raise RuntimeError("DEMO_REFERENCE_ENERGY_MISMATCH")
    scenario = ScenarioInput(
        scenario_version="historical-flex-scenario-v1",
        profile_content_sha256=locked.content.sha256(),
        tariff_version_id="pge-etoud-2026-07",
        profile_slots=slots,
        loads=(
            FlexibleLoad(
                load_id=UUID("00000000-0000-0000-0000-000000000001"),
                physical_asset_key="public-demo-ev",
                kind=cast(Any, template["kind"]),
                mode=cast(Any, template["mode"]),
                execution_spec=InterruptibleModulatingSpec(
                    execution_type="INTERRUPTIBLE_MODULATING",
                    maximum_power_w=cast(int, template["maximum_power_w"]),
                    minimum_power_when_active_w=cast(int, template["minimum_power_when_active_w"]),
                ),
                occurrences=(
                    LoadOccurrence(
                        occurrence_id=UUID("10000000-0000-0000-0000-000000000001"),
                        required_energy_wh=required_energy_wh,
                        earliest_start_utc=datetime.fromisoformat(
                            cast(str, occurrence["earliest_start_utc"]).replace("Z", "+00:00")
                        ),
                        deadline_utc=datetime.fromisoformat(
                            cast(str, occurrence["deadline_utc"]).replace("Z", "+00:00")
                        ),
                        reference_schedule=reference,
                    ),
                ),
            ),
        ),
        electrical_constraints=ScenarioElectricalConstraints.model_validate(
            workload["electrical_constraints"]
        ),
    )
    admitted = next(
        item for item in tariffs if item.lock.tariff_version_id == scenario.tariff_version_id
    )
    validated = validate_and_decompose_scenario(scenario)
    configuration = default_solver_configuration(max_deterministic_time_per_stage=5.0)
    exact = optimize_exact(
        validated,
        admitted.compilation,
        account,
        dated_facts=dated,
        configuration=configuration,
    )
    heuristic = optimize_off_peak_heuristic(
        validated,
        admitted.compilation,
        account,
        dated_facts=dated,
        configuration=configuration,
    )
    result = build_scenario_result(
        validated,
        admitted.compilation,
        account,
        dated,
        exact,
        heuristic,
    )
    if result.exact.search_status != "OPTIMAL":
        raise RuntimeError(f"DEMO_EXACT_STATUS:{result.exact.search_status}")
    if result.exact.selected.verification.status != "VALID":
        raise RuntimeError("DEMO_SELECTED_SCHEDULE_UNVERIFIED")
    return result


def _scenario_artifacts(result: ScenarioOptimizationResult) -> dict[str, object]:
    reference = result.exact.reference.schedule.occurrences[0]
    selected = result.exact.selected.schedule.occurrences[0]
    heuristic = result.heuristic.selected.schedule.occurrences[0]
    window_start = datetime.fromisoformat("2026-07-07T00:00:00+00:00")
    window_end = datetime.fromisoformat("2026-07-07T07:00:00+00:00")
    heatmap_slots: list[dict[str, object]] = []
    for index, reference_slot in enumerate(reference.slots):
        if not window_start <= reference_slot.slot_start_utc < window_end:
            continue
        selected_slot = selected.slots[index]
        heuristic_slot = heuristic.slots[index]
        heatmap_slots.append(
            {
                "slot_start_utc": reference_slot.slot_start_utc.isoformat(),
                "duration_seconds": reference_slot.duration_seconds,
                "reference_energy_wh": reference_slot.energy_wh,
                "heuristic_energy_wh": heuristic_slot.energy_wh,
                "selected_energy_wh": selected_slot.energy_wh,
            }
        )
    decomposition = result.decomposition
    common = {
        "calculation_time_mode": result.calculation_time_mode,
        "historical_addition_label": result.historical_addition_label,
        "tariff_version_id": result.manifest.tariff_version_id,
        "profile_content_sha256": result.manifest.profile_content_sha256,
        "heatmap_slots": heatmap_slots,
    }
    return {
        "scenario-inputs": {
            **common,
            "load": {
                "kind": "EV",
                "mode": "HISTORICAL_ADDITION",
                "execution_type": "INTERRUPTIBLE_MODULATING",
                "required_energy_wh": sum(slot.energy_wh for slot in reference.slots),
                "maximum_power_w": 7_200,
                "minimum_power_when_active_w": 0,
                "earliest_start_utc": window_start.isoformat(),
                "deadline_utc": window_end.isoformat(),
            },
            "reference_validation": result.reference_validation.model_dump(mode="json"),
            "decomposition": {
                "fixed_background_wh": sum(
                    slot.energy_wh for slot in decomposition.fixed_background
                ),
                "existing_load_reference_wh": sum(
                    slot.energy_wh for slot in decomposition.shift_existing_reference
                ),
                "historical_addition_reference_wh": sum(
                    slot.energy_wh for slot in decomposition.historical_addition_reference
                ),
                "reconstructed_measured_profile_wh": sum(
                    slot.energy_wh for slot in decomposition.reconstructed_measured_profile
                ),
                "unchanged_reference_profile_wh": sum(
                    slot.energy_wh for slot in decomposition.unchanged_reference_profile
                ),
                "exact_measured_reconstruction": (decomposition.exact_measured_reconstruction),
            },
        },
        "reference-result": {
            **common,
            "supported_cost_cents": (
                result.exact.reference.billing_result.supported_calculated_cents
            ),
            "objective": result.exact.reference.verification.objective.model_dump(mode="json"),
            "verification_status": result.exact.reference.verification.status,
            "verification_sha256": (result.exact.reference.verification.verification_sha256),
        },
        "heuristic-result": {
            **common,
            "search_status": result.heuristic.search_status,
            "selection_outcome": result.heuristic.selection_outcome,
            "bill_optimality_claim": result.heuristic.bill_optimality_claim,
            "fallback_reason": result.heuristic.fallback_reason,
            "supported_cost_cents": (
                result.heuristic.selected.billing_result.supported_calculated_cents
            ),
            "objective": result.heuristic.selected.verification.objective.model_dump(mode="json"),
            "rank_calendar_sha256": result.manifest.rank_calendar_sha256,
        },
        "solver-result": {
            **common,
            "search_status": result.exact.search_status,
            "selected_source": result.exact.selected_source,
            "selection_reason": result.exact.selection_reason,
            "supported_cost_cents": (
                result.exact.selected.billing_result.supported_calculated_cents
            ),
            "reference_supported_cost_cents": (
                result.exact.reference.billing_result.supported_calculated_cents
            ),
            "objective": result.exact.selected.verification.objective.model_dump(mode="json"),
            "highest_objective_stage_proved_optimal": (
                result.exact.highest_objective_stage_proved_optimal
            ),
            "first_open_stage": result.exact.first_open_stage,
            "absolute_cost_gap_cents": result.exact.absolute_cost_gap_cents,
            "relative_cost_gap": result.exact.relative_cost_gap,
            "result_sha256": result.result_sha256,
            "calculation_sha256": result.manifest.calculation_sha256,
        },
        "verification-record": {
            "status": result.exact.selected.verification.status,
            "verification_version": (result.exact.selected.verification.verification_version),
            "objective": result.exact.selected.verification.objective.model_dump(mode="json"),
            "verification_sha256": (result.exact.selected.verification.verification_sha256),
            "scenario_result_sha256": result.result_sha256,
            "warning_codes": result.manifest.warning_codes,
        },
        "redacted-report": build_redacted_report(result).model_dump(mode="json"),
    }


def build_artifacts() -> dict[str, object]:
    locked = load_locked_simulated_profile(ROOT)
    account, dated = _facts()
    tariffs = load_all_admitted_tariffs(ROOT)
    current = next(item for item in tariffs if item.lock.tariff_version_id == "pge-e1-2026-07")
    replay = replay_compiled_tariff(
        current.compilation,
        ReplayRequest(
            request_version="e1-replay-request-v1",
            profile_content_sha256=locked.content.sha256(),
            account_facts=account,
            energy_wh=_total_energy_wh(),
            current_bill_total_cents=30_000,
            user_unsupported_lines=(
                UserUnsupportedLine(
                    line_item_key="simulated_local_tax",
                    description="Simulated current-bill local tax",
                    amount_cents=300,
                ),
            ),
        ),
    )
    comparison = compare_admitted_tariffs(
        tariffs,
        _interval_request(account, dated),
        current_tariff_version_id="pge-e1-2026-07",
        required_component_keys=load_required_component_keys(ROOT),
    )
    if not comparison.rankable:
        raise RuntimeError("DEMO_COMPARISON_NOT_RANKABLE")
    scenario = _scenario(account, dated, tariffs)
    scenario_artifacts = _scenario_artifacts(scenario)
    return {
        "import-review": {
            "label": locked.label,
            "simulated": True,
            "source_artifact_path": locked.artifact_path,
            "source_artifact_sha256": locked.artifact_sha256,
            "profile_content_sha256": locked.content.sha256(),
            "parser_contract_version": locked.content.parser_contract_version,
            "adapter_fingerprint": locked.content.adapter_fingerprint,
            "reading_count": len(locked.content.readings),
            "interval_resolution_seconds": locked.content.interval_resolution_seconds,
            "coverage_start_utc_ns": locked.content.billing_period_start_utc_ns,
            "coverage_end_utc_ns": locked.content.billing_period_end_utc_ns,
            "total_energy_wh": _total_energy_wh(),
            "findings": [],
            "quality_status": "READY",
        },
        "bill-replay": replay.model_dump(mode="json"),
        "tariff-comparison": comparison.model_dump(mode="json"),
        **scenario_artifacts,
    }


def _release_files(artifacts: dict[str, object]) -> tuple[dict[str, bytes], bytes, bytes]:
    if tuple(artifacts) != REQUIRED_LOGICAL_IDS:
        raise RuntimeError("DEMO_LOGICAL_ID_SET_MISMATCH")
    object_files: dict[str, bytes] = {}
    entries: list[dict[str, str]] = []
    for logical_id, payload in artifacts.items():
        content = _canonical_bytes(
            {
                "schema_version": "public-demo-artifact-v1",
                "logical_id": logical_id,
                "simulated": True,
                "payload": payload,
            }
        )
        digest = _sha256(content)
        relative = f"objects/{digest}.json"
        object_files[relative] = content
        entries.append(
            {
                "logical_id": logical_id,
                "media_type": "application/json",
                "path": relative,
                "sha256": digest,
            }
        )
    allowlist = _canonical_bytes(
        {
            "allowlist_version": "public-demo-allowlist-v1",
            "logical_artifact_ids": list(REQUIRED_LOGICAL_IDS),
            "permitted_media_types": ["application/json", "application/pdf"],
            "prohibited_capabilities": [
                "anonymous-api-mutation",
                "authenticated-api-request",
                "job-request",
                "private-data-reference",
                "server-side-visitor-state",
                "shared-account",
                "upload",
            ],
        }
    )
    manifest = _canonical_bytes(
        {
            "manifest_version": "public-demo-manifest-v1",
            "generation_command": "make demo-artifacts",
            "simulated_only": True,
            "allowlist_sha256": _sha256(allowlist),
            "calculation_manifest_sha256": cast(
                str,
                cast(dict[str, Any], artifacts["solver-result"])["calculation_sha256"],
            ),
            "artifacts": entries,
        }
    )
    return object_files, allowlist, manifest


def _typescript_lock(manifest: bytes) -> bytes:
    return (
        "// Generated by scripts/generate_demo_artifacts.py. Do not edit.\n"
        "export const DEMO_MANIFEST_SHA256 =\n"
        f'  "{_sha256(manifest)}";\n'
    ).encode("ascii")


def _write_release(output: Path, typescript_lock: Path) -> None:
    objects, allowlist, manifest = _release_files(build_artifacts())
    object_directory = output / "objects"
    object_directory.mkdir(parents=True, exist_ok=True)
    for stale in object_directory.glob("*.json"):
        stale.unlink()
    for relative, content in objects.items():
        path = output / relative
        path.write_bytes(content)
    (output / "allowlist.v1.json").write_bytes(allowlist)
    (output / "manifest.v1.json").write_bytes(manifest)
    (output / "release.v1.json").unlink(missing_ok=True)
    typescript_lock.parent.mkdir(parents=True, exist_ok=True)
    typescript_lock.write_bytes(_typescript_lock(manifest))


def _compare_tree(expected: Path, observed: Path) -> None:
    ignored = {Path("manifest.schema.json")}
    expected_files = {
        path.relative_to(expected): path.read_bytes()
        for path in expected.rglob("*")
        if path.is_file()
    }
    observed_files = {
        path.relative_to(observed): path.read_bytes()
        for path in observed.rglob("*")
        if path.is_file() and path.relative_to(observed) not in ignored
    }
    if expected_files != observed_files:
        missing = sorted(set(expected_files).difference(observed_files))
        extra = sorted(set(observed_files).difference(expected_files))
        changed = sorted(
            path
            for path in set(expected_files).intersection(observed_files)
            if expected_files[path] != observed_files[path]
        )
        raise RuntimeError(
            f"DEMO_ARTIFACTS_STALE:missing={missing}:extra={extra}:changed={changed}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if not arguments.check:
        _write_release(OUTPUT, TYPESCRIPT_LOCK)
        print("Generated the content-addressed public demo release.")
        return
    with tempfile.TemporaryDirectory(prefix="ratereplay-demo-") as directory:
        root = Path(directory)
        generated = root / "demo"
        lock = root / "demoReleaseLock.ts"
        _write_release(generated, lock)
        _compare_tree(generated, OUTPUT)
        if lock.read_bytes() != TYPESCRIPT_LOCK.read_bytes():
            raise RuntimeError("DEMO_RELEASE_LOCK_STALE")
    print("Public demo artifacts are reproducible and current.")


if __name__ == "__main__":
    main()
