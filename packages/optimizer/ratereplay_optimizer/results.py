"""Immutable, deterministic scenario result and manifest contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from ratereplay_tariffs.billing import ReplayResult
from ratereplay_tariffs.compiled import CompilationBundle
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

from ratereplay_optimizer.models import (
    CandidateSchedule,
    FrozenModel,
    HeuristicStageRecord,
    LoweringRecord,
    ReferenceValidationRecord,
    ScenarioDecomposition,
    SolverConfiguration,
    SolverStageRecord,
    ValidatedScenario,
    VerificationRecord,
)
from ratereplay_optimizer.solver import ExactOptimizationResult, HeuristicOptimizationResult
from ratereplay_optimizer.verification import VerifiedSchedule


class ScenarioResultError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VerifiedScheduleResult(FrozenModel):
    schedule: CandidateSchedule
    verification: VerificationRecord
    billing_result: ReplayResult


class ExactScenarioResult(FrozenModel):
    search_status: Literal["OPTIMAL", "BEST_FOUND"]
    selected_source: Literal["SOLVER_INCUMBENT", "REFERENCE"]
    selection_reason: Literal[
        "INCUMBENT_STRICTLY_BETTER",
        "REFERENCE_EQUAL_OR_BETTER",
        "NO_VERIFIED_INCUMBENT",
    ]
    selected: VerifiedScheduleResult
    incumbent: VerifiedScheduleResult | None
    reference: VerifiedScheduleResult
    stage_records: tuple[SolverStageRecord, ...]
    highest_objective_stage_proved_optimal: int = Field(ge=0, le=4)
    first_open_stage: int | None = Field(default=None, ge=1, le=4)
    best_supported_cost_bound: float | None
    absolute_cost_gap_cents: float | None
    relative_cost_gap: float | None
    solver_configuration: SolverConfiguration
    lowering_record: LoweringRecord
    execution_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HeuristicScenarioResult(FrozenModel):
    heuristic_contract_version: Literal["off-peak-heuristic-v1"] = "off-peak-heuristic-v1"
    bill_optimality_claim: Literal[False] = False
    search_status: Literal[
        "HEURISTIC_PROXY_OPTIMAL",
        "HEURISTIC_BEST_FOUND",
        "HEURISTIC_NO_INCUMBENT",
    ]
    selection_outcome: Literal[
        "HEURISTIC_INCUMBENT_SELECTED",
        "HEURISTIC_REFERENCE_DOMINATES",
        "HEURISTIC_REFERENCE_FALLBACK",
    ]
    selected: VerifiedScheduleResult
    incumbent: VerifiedScheduleResult | None
    reference: VerifiedScheduleResult
    incumbent_proxy_pair: tuple[int, int] | None
    reference_proxy_pair: tuple[int, int]
    stage_records: tuple[HeuristicStageRecord, ...]
    solver_configuration: SolverConfiguration
    lowering_record: LoweringRecord
    fallback_reason: str | None
    execution_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScenarioLoadManifest(FrozenModel):
    load_id: str
    mode: Literal["SHIFT_EXISTING", "HISTORICAL_ADDITION"]
    reference_schedule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScenarioCalculationManifest(FrozenModel):
    manifest_version: Literal["scenario-calculation-manifest-v1"] = (
        "scenario-calculation-manifest-v1"
    )
    calculation_contract_version: Literal["verified-scenario-calculation-v1"] = (
        "verified-scenario-calculation-v1"
    )
    calculation_schema_version: Literal["scenario-result-v1"] = "scenario-result-v1"
    calculation_time_mode: Literal["HISTORICAL_REPLAY"] = "HISTORICAL_REPLAY"
    historical_addition_label: Literal["HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"] = (
        "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"
    )
    profile_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tariff_version_id: str
    tariff_compiler_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tariff_ir_version: str
    tariff_source_hashes: tuple[str, ...]
    account_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_validation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    load_modes_and_reference_hashes: tuple[ScenarioLoadManifest, ...]
    solver_name: str
    solver_version: str
    solver_configuration: SolverConfiguration
    solver_lowering_capability_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    solver_lowering_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invariance_proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_execution_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heuristic_contract_version: Literal["off-peak-heuristic-v1"] = "off-peak-heuristic-v1"
    rank_calendar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    heuristic_execution_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_verification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_verification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    warning_codes: tuple[str, ...]
    calculation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScenarioOptimizationResult(FrozenModel):
    result_version: Literal["scenario-result-v1"] = "scenario-result-v1"
    calculation_time_mode: Literal["HISTORICAL_REPLAY"] = "HISTORICAL_REPLAY"
    historical_addition_label: Literal["HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"] = (
        "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"
    )
    reference_validation: ReferenceValidationRecord
    decomposition: ScenarioDecomposition
    exact: ExactScenarioResult
    heuristic: HeuristicScenarioResult
    manifest: ScenarioCalculationManifest
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _stored_schedule(value: VerifiedSchedule) -> VerifiedScheduleResult:
    return VerifiedScheduleResult(
        schedule=value.schedule,
        verification=value.record,
        billing_result=value.billing_result,
    )


def _exact_result(value: ExactOptimizationResult) -> ExactScenarioResult:
    if value.search_status not in {"OPTIMAL", "BEST_FOUND"}:
        raise ScenarioResultError(
            f"EXACT_SOLVER_{value.search_status}",
            "The exact solver did not produce a successful verified schedule.",
        )
    return ExactScenarioResult(
        search_status=value.search_status,
        selected_source=value.selected.selected_source,
        selection_reason=value.selected.reason,
        selected=_stored_schedule(value.selected.selected),
        incumbent=(
            _stored_schedule(value.selected.incumbent)
            if value.selected.incumbent is not None
            else None
        ),
        reference=_stored_schedule(value.selected.reference),
        stage_records=value.stage_records,
        highest_objective_stage_proved_optimal=value.highest_objective_stage_proved_optimal,
        first_open_stage=value.first_open_stage,
        best_supported_cost_bound=value.best_supported_cost_bound,
        absolute_cost_gap_cents=value.absolute_cost_gap_cents,
        relative_cost_gap=value.relative_cost_gap,
        solver_configuration=value.solver_configuration,
        lowering_record=value.lowering_record,
        execution_result_sha256=value.result_sha256,
    )


def _heuristic_result(value: HeuristicOptimizationResult) -> HeuristicScenarioResult:
    allowed = {
        "HEURISTIC_PROXY_OPTIMAL",
        "HEURISTIC_BEST_FOUND",
        "HEURISTIC_NO_INCUMBENT",
    }
    if value.search_status not in allowed:
        raise ScenarioResultError(
            value.search_status,
            "The heuristic terminated with a fail-closed internal status.",
        )
    return HeuristicScenarioResult.model_validate(
        {
            "search_status": value.search_status,
            "selection_outcome": value.selection_outcome,
            "selected": _stored_schedule(value.selected),
            "incumbent": _stored_schedule(value.incumbent) if value.incumbent else None,
            "reference": _stored_schedule(value.reference),
            "incumbent_proxy_pair": value.incumbent_proxy_pair,
            "reference_proxy_pair": value.reference_proxy_pair,
            "stage_records": value.stage_records,
            "solver_configuration": value.solver_configuration,
            "lowering_record": value.lowering_record,
            "fallback_reason": value.fallback_reason,
            "execution_result_sha256": value.result_sha256,
        }
    )


def build_scenario_result(
    validated: ValidatedScenario,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    dated_facts: DatedEligibilityFacts | None,
    exact: ExactOptimizationResult,
    heuristic: HeuristicOptimizationResult,
) -> ScenarioOptimizationResult:
    """Build a successful scenario resource from independently verified executions."""

    exact_result = _exact_result(exact)
    heuristic_result = _heuristic_result(heuristic)
    scenario_payload = validated.scenario.model_dump(mode="json")
    scenario_hash = canonical_content_sha256(b"RateReplay.ScenarioInput.v1", scenario_payload)
    reference_validation_hash = canonical_content_sha256(
        b"RateReplay.ReferenceValidation.v1",
        validated.reference_validation.model_dump(mode="json"),
    )
    account_hash = canonical_content_sha256(
        b"RateReplay.ScenarioEligibilityFacts.v1",
        {
            "account_facts": account_facts.model_dump(mode="json"),
            "dated_eligibility_facts": (
                dated_facts.model_dump(mode="json") if dated_facts is not None else None
            ),
        },
    )
    load_manifests = tuple(
        ScenarioLoadManifest(
            load_id=str(load.load_id),
            mode=load.mode,
            reference_schedule_sha256=canonical_content_sha256(
                b"RateReplay.LoadReferenceSchedules.v1",
                {
                    "load_id": str(load.load_id),
                    "occurrences": [
                        {
                            "occurrence_id": str(occurrence.occurrence_id),
                            "reference_schedule": [
                                slot.model_dump(mode="json")
                                for slot in occurrence.reference_schedule
                            ],
                        }
                        for occurrence in load.occurrences
                    ],
                },
            ),
        )
        for load in validated.scenario.loads
    )
    capability_hash = canonical_content_sha256(
        b"RateReplay.SolverLoweringCapability.v1",
        {
            "supported_operators": bundle.reports.solver_lowering_supported_operators,
            "unsupported_reasons": bundle.reports.solver_lowering_unsupported_reasons,
        },
    )
    proof_hash = canonical_content_sha256(
        b"RateReplay.SolverInvarianceProofs.v1",
        [proof.model_dump(mode="json") for proof in exact.lowering_record.omitted_charge_proofs],
    )
    manifest_payload = {
        "calculation_contract_version": "verified-scenario-calculation-v1",
        "calculation_schema_version": "scenario-result-v1",
        "profile_content_sha256": validated.scenario.profile_content_sha256,
        "tariff_version_id": bundle.ir.tariff_version_id,
        "tariff_compiler_content_sha256": bundle.compiler_content_sha256,
        "tariff_ir_version": bundle.ir.ir_version,
        "tariff_source_hashes": tuple(
            sorted(source.source_sha256 for source in bundle.reports.source_coverage)
        ),
        "account_facts_sha256": account_hash,
        "scenario_input_sha256": scenario_hash,
        "reference_validation_sha256": reference_validation_hash,
        "load_modes_and_reference_hashes": tuple(
            item.model_dump(mode="json") for item in load_manifests
        ),
        "solver_configuration": exact.solver_configuration.model_dump(mode="json"),
        "solver_lowering_capability_sha256": capability_hash,
        "solver_lowering_sha256": exact.lowering_record.lowering_sha256,
        "invariance_proof_sha256": proof_hash,
        "exact_execution_result_sha256": exact.result_sha256,
        "heuristic_contract_version": "off-peak-heuristic-v1",
        "rank_calendar_sha256": heuristic.lowering_record.rank_calendar_sha256,
        "heuristic_execution_result_sha256": heuristic.result_sha256,
        "selected_verification_sha256": exact.selected.selected.record.verification_sha256,
        "reference_verification_sha256": exact.selected.reference.record.verification_sha256,
        "warning_codes": (
            ("EXACT_BEST_FOUND_OPEN_BOUND",) if exact.search_status == "BEST_FOUND" else ()
        ),
    }
    calculation_hash = canonical_content_sha256(
        b"RateReplay.ScenarioCalculation.v1", manifest_payload
    )
    manifest = ScenarioCalculationManifest.model_validate(
        {
            **manifest_payload,
            "solver_name": exact.solver_configuration.solver_name,
            "solver_version": exact.solver_configuration.solver_version,
            "calculation_sha256": calculation_hash,
        }
    )
    result_payload = {
        "reference_validation": validated.reference_validation.model_dump(mode="json"),
        "decomposition": validated.decomposition.model_dump(mode="json"),
        "exact": exact_result.model_dump(mode="json"),
        "heuristic": heuristic_result.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
    }
    return ScenarioOptimizationResult(
        reference_validation=validated.reference_validation,
        decomposition=validated.decomposition,
        exact=exact_result,
        heuristic=heuristic_result,
        manifest=manifest,
        result_sha256=canonical_content_sha256(b"RateReplay.ScenarioResult.v1", result_payload),
    )
