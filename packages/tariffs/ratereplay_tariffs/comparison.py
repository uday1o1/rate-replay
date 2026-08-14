"""Fail-closed comparable-cost compilation and alternative-plan replay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.billing import (
    ChargeLineItem,
    EligibilityResult,
    IntervalCalculationManifest,
    IntervalReplayRequest,
    ReplayError,
    UnsupportedPlaceholder,
    evaluate_eligibility,
    replay_compiled_tariff,
)
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import ChargeComponentKey, FrozenModel

CoverageStatus = Literal["SUPPORTED", "NOT_APPLICABLE", "BLOCKED"]
ExclusionCode = Literal[
    "CANDIDATE_ELIGIBILITY_UNKNOWN",
    "CANDIDATE_INELIGIBLE",
    "INCOMPLETE_COMPONENT_VECTOR",
    "UNSUPPORTED_COMPONENT",
    "UNCLASSIFIED_ACTIVE_COMPONENT",
]
_CHARGE_COMPONENT_KEYS: frozenset[str] = frozenset(
    {
        "baseline_allowance",
        "bundled_energy",
        "baseline_adjustment",
        "base_services_charge",
        "california_climate_credit",
        "minimum_bill_adjustment",
        "explicit_unsupported",
    }
)


class ComparisonError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ComponentCoverage(FrozenModel):
    component_key: str
    status: CoverageStatus
    reason_code: str | None = None
    contributing_rule_ids: tuple[str, ...] = ()


class ComparisonExclusion(FrozenModel):
    code: ExclusionCode
    tariff_version_id: str
    component_key: str | None = None
    eligibility_reason_codes: tuple[str, ...] = ()


class AlternativePlanResult(FrozenModel):
    result_version: Literal["alternative-plan-result-v1"] = "alternative-plan-result-v1"
    tariff_version_id: str
    plan_code: str
    supported_calculated_cents: int
    line_items: tuple[ChargeLineItem, ...]
    tariff_unsupported_placeholders: tuple[UnsupportedPlaceholder, ...]
    component_coverage: tuple[ComponentCoverage, ...]
    provenance_sources: tuple[dict[str, object], ...]
    manifest: IntervalCalculationManifest
    result_sha256: str


class ComparisonCandidateResult(FrozenModel):
    tariff_version_id: str
    plan_code: str
    eligibility: EligibilityResult
    component_coverage: tuple[ComponentCoverage, ...]
    alternative_plan: AlternativePlanResult | None


class ComparisonResult(FrozenModel):
    comparison_version: Literal["tariff-comparison-result-v1"] = "tariff-comparison-result-v1"
    calculation_time_mode: Literal["HISTORICAL_REPLAY"] = "HISTORICAL_REPLAY"
    profile_content_sha256: str
    current_tariff_version_id: str
    required_component_keys: tuple[ChargeComponentKey, ...]
    common_supported_component_keys: tuple[ChargeComponentKey, ...]
    candidates: tuple[ComparisonCandidateResult, ...]
    exclusions: tuple[ComparisonExclusion, ...]
    rankable: bool
    ranked_tariff_version_ids: tuple[str, ...]
    winner_tariff_version_ids: tuple[str, ...]
    savings_against_current_supported_cents: int | None
    comparison_sha256: str


def load_required_component_keys(root: Path) -> tuple[ChargeComponentKey, ...]:
    """Load and canonicalize the locked comparable-cost component universe."""

    path = root / "tariffs/admission/candidate-matrix-v1.json"
    try:
        payload = cast(dict[str, Any], json.loads(path.read_bytes()))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise ComparisonError(
            "COMPARISON_MATRIX_INVALID", "Comparison component matrix is unreadable"
        ) from error
    raw = payload.get("comparison_component_keys")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(value, str) and value in _CHARGE_COMPONENT_KEYS for value in raw)
        or len(raw) != len(set(raw))
    ):
        raise ComparisonError("COMPARISON_MATRIX_INVALID", "Comparison component matrix is invalid")
    return cast(tuple[ChargeComponentKey, ...], tuple(sorted(raw)))


def _declared_components(admitted: AdmittedTariff) -> tuple[ChargeComponentKey, ...]:
    raw = admitted.compilation.normalized_ast.get("comparison_component_keys")
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise ComparisonError(
            "COMPARISON_COMPONENT_REPORT_INVALID",
            f"{admitted.lock.tariff_version_id} has no valid comparison component report",
        )
    return cast(tuple[ChargeComponentKey, ...], tuple(raw))


def _coverage(
    admitted: AdmittedTariff,
    required_components: tuple[ChargeComponentKey, ...],
    *,
    line_items: tuple[ChargeLineItem, ...] = (),
    placeholders: tuple[UnsupportedPlaceholder, ...] = (),
) -> tuple[ComponentCoverage, ...]:
    declared = set(_declared_components(admitted))
    active_rule_ids: dict[str, set[str]] = {}
    for line in line_items:
        active_rule_ids.setdefault(line.charge_component_key, set()).add(line.rule_id)
    coverage: list[ComponentCoverage] = []
    for component in required_components:
        rule_ids = tuple(sorted(active_rule_ids.get(component, set())))
        if component in declared:
            coverage.append(
                ComponentCoverage(
                    component_key=component,
                    status="SUPPORTED",
                    contributing_rule_ids=rule_ids,
                )
            )
        elif rule_ids:
            coverage.append(
                ComponentCoverage(
                    component_key=component,
                    status="BLOCKED",
                    reason_code="UNCLASSIFIED_ACTIVE_COMPONENT",
                    contributing_rule_ids=rule_ids,
                )
            )
        else:
            coverage.append(ComponentCoverage(component_key=component, status="NOT_APPLICABLE"))
    for active_component, active_rules in sorted(active_rule_ids.items()):
        if active_component not in required_components:
            coverage.append(
                ComponentCoverage(
                    component_key=active_component,
                    status="BLOCKED",
                    reason_code="UNCLASSIFIED_ACTIVE_COMPONENT",
                    contributing_rule_ids=tuple(sorted(active_rules)),
                )
            )
    for placeholder in placeholders:
        coverage.append(
            ComponentCoverage(
                component_key="explicit_unsupported",
                status="BLOCKED",
                reason_code=placeholder.reason_code,
                contributing_rule_ids=(placeholder.rule_id,),
            )
        )
    return tuple(sorted(coverage, key=lambda item: (item.component_key, item.reason_code or "")))


def _alternative_result(
    admitted: AdmittedTariff,
    request: IntervalReplayRequest,
    required_components: tuple[ChargeComponentKey, ...],
) -> AlternativePlanResult:
    if request.current_bill_total_cents is not None or request.user_unsupported_lines:
        raise ComparisonError(
            "ALTERNATIVE_RECONCILIATION_FORBIDDEN",
            "Alternative-plan replay cannot consume current-bill reconciliation inputs",
        )
    try:
        replay = replay_compiled_tariff(admitted.compilation, request)
    except ReplayError as error:
        raise ComparisonError(
            "CANDIDATE_REPLAY_FAILED",
            f"{admitted.lock.tariff_version_id} replay failed with {error.code}",
        ) from error
    if not isinstance(replay.manifest, IntervalCalculationManifest):
        raise ComparisonError(
            "INTERVAL_MANIFEST_REQUIRED", "Alternative-plan replay requires interval evidence"
        )
    coverage = _coverage(
        admitted,
        required_components,
        line_items=replay.line_items,
        placeholders=replay.tariff_unsupported_placeholders,
    )
    payload = {
        "tariff_version_id": admitted.lock.tariff_version_id,
        "plan_code": admitted.lock.plan_code,
        "supported_calculated_cents": replay.supported_calculated_cents,
        "line_items": [line.model_dump(mode="json") for line in replay.line_items],
        "tariff_unsupported_placeholders": [
            item.model_dump(mode="json") for item in replay.tariff_unsupported_placeholders
        ],
        "component_coverage": [item.model_dump(mode="json") for item in coverage],
        "provenance_sources": replay.provenance_sources,
        "manifest": replay.manifest.model_dump(mode="json"),
    }
    return AlternativePlanResult(
        tariff_version_id=admitted.lock.tariff_version_id,
        plan_code=admitted.lock.plan_code,
        supported_calculated_cents=replay.supported_calculated_cents,
        line_items=replay.line_items,
        tariff_unsupported_placeholders=replay.tariff_unsupported_placeholders,
        component_coverage=coverage,
        provenance_sources=replay.provenance_sources,
        manifest=replay.manifest,
        result_sha256=canonical_content_sha256(b"RateReplay.AlternativePlanResult.v1", payload),
    )


def compare_admitted_tariffs(
    admitted_tariffs: tuple[AdmittedTariff, ...],
    request: IntervalReplayRequest,
    *,
    current_tariff_version_id: str,
    required_component_keys: tuple[ChargeComponentKey, ...],
) -> ComparisonResult:
    """Replay one immutable profile across candidates and rank only complete coverage."""

    if len(admitted_tariffs) < 2:
        raise ComparisonError("TOO_FEW_CANDIDATES", "Comparison requires at least two tariffs")
    ordered = tuple(sorted(admitted_tariffs, key=lambda item: item.lock.tariff_version_id))
    tariff_ids = tuple(item.lock.tariff_version_id for item in ordered)
    if len(set(tariff_ids)) != len(tariff_ids):
        raise ComparisonError("DUPLICATE_CANDIDATE", "Comparison candidates must be unique")
    if current_tariff_version_id not in tariff_ids:
        raise ComparisonError(
            "CURRENT_TARIFF_NOT_CANDIDATE", "Current replay tariff must be a candidate"
        )
    if tuple(sorted(required_component_keys)) != required_component_keys or len(
        set(required_component_keys)
    ) != len(required_component_keys):
        raise ComparisonError(
            "COMPARISON_COMPONENTS_NONCANONICAL",
            "Required comparison components must be unique and sorted",
        )

    candidates: list[ComparisonCandidateResult] = []
    exclusions: list[ComparisonExclusion] = []
    for admitted in ordered:
        eligibility = evaluate_eligibility(
            admitted.compilation,
            request.account_facts,
            request.dated_eligibility_facts,
        )
        alternative: AlternativePlanResult | None = None
        coverage = _coverage(admitted, required_component_keys)
        vector = admitted.compilation.reports.component_vector
        if any(count != 1 for count in vector.active_component_count_by_key):
            exclusions.append(
                ComparisonExclusion(
                    code="INCOMPLETE_COMPONENT_VECTOR",
                    tariff_version_id=admitted.lock.tariff_version_id,
                )
            )
        if eligibility.status == "ELIGIBLE":
            alternative = _alternative_result(admitted, request, required_component_keys)
            coverage = alternative.component_coverage
        elif eligibility.status == "UNKNOWN":
            exclusions.append(
                ComparisonExclusion(
                    code="CANDIDATE_ELIGIBILITY_UNKNOWN",
                    tariff_version_id=admitted.lock.tariff_version_id,
                    eligibility_reason_codes=eligibility.reason_codes,
                )
            )
        else:
            exclusions.append(
                ComparisonExclusion(
                    code="CANDIDATE_INELIGIBLE",
                    tariff_version_id=admitted.lock.tariff_version_id,
                    eligibility_reason_codes=eligibility.reason_codes,
                )
            )
        for item in coverage:
            if item.status == "BLOCKED":
                exclusions.append(
                    ComparisonExclusion(
                        code=(
                            "UNCLASSIFIED_ACTIVE_COMPONENT"
                            if item.reason_code == "UNCLASSIFIED_ACTIVE_COMPONENT"
                            else "UNSUPPORTED_COMPONENT"
                        ),
                        tariff_version_id=admitted.lock.tariff_version_id,
                        component_key=item.component_key,
                    )
                )
        candidates.append(
            ComparisonCandidateResult(
                tariff_version_id=admitted.lock.tariff_version_id,
                plan_code=admitted.lock.plan_code,
                eligibility=eligibility,
                component_coverage=coverage,
                alternative_plan=alternative,
            )
        )

    exclusions_tuple = tuple(
        sorted(
            set(exclusions),
            key=lambda item: (
                item.tariff_version_id,
                item.code,
                item.component_key or "",
                item.eligibility_reason_codes,
            ),
        )
    )
    rankable = not exclusions_tuple and all(
        candidate.alternative_plan is not None for candidate in candidates
    )
    ranked_ids: tuple[str, ...] = ()
    winner_ids: tuple[str, ...] = ()
    savings: int | None = None
    if rankable:
        ranked = sorted(
            candidates,
            key=lambda item: (
                cast(AlternativePlanResult, item.alternative_plan).supported_calculated_cents,
                item.plan_code,
                item.tariff_version_id,
            ),
        )
        ranked_ids = tuple(item.tariff_version_id for item in ranked)
        lowest_cost = cast(
            AlternativePlanResult, ranked[0].alternative_plan
        ).supported_calculated_cents
        winner_ids = tuple(
            item.tariff_version_id
            for item in ranked
            if cast(AlternativePlanResult, item.alternative_plan).supported_calculated_cents
            == lowest_cost
        )
        current = next(
            item for item in candidates if item.tariff_version_id == current_tariff_version_id
        )
        current_cost = cast(
            AlternativePlanResult, current.alternative_plan
        ).supported_calculated_cents
        savings = current_cost - lowest_cost

    declared_sets = [set(_declared_components(admitted)) for admitted in ordered]
    common_supported = tuple(
        component
        for component in required_component_keys
        if all(component in declared for declared in declared_sets)
    )
    result_payload = {
        "profile_content_sha256": request.profile_content_sha256,
        "current_tariff_version_id": current_tariff_version_id,
        "required_component_keys": required_component_keys,
        "common_supported_component_keys": common_supported,
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        "exclusions": [item.model_dump(mode="json") for item in exclusions_tuple],
        "rankable": rankable,
        "ranked_tariff_version_ids": ranked_ids,
        "winner_tariff_version_ids": winner_ids,
        "savings_against_current_supported_cents": savings,
    }
    return ComparisonResult(
        profile_content_sha256=request.profile_content_sha256,
        current_tariff_version_id=current_tariff_version_id,
        required_component_keys=required_component_keys,
        common_supported_component_keys=common_supported,
        candidates=tuple(candidates),
        exclusions=exclusions_tuple,
        rankable=rankable,
        ranked_tariff_version_ids=ranked_ids,
        winner_tariff_version_ids=winner_ids,
        savings_against_current_supported_cents=savings,
        comparison_sha256=canonical_content_sha256(
            b"RateReplay.TariffComparisonResult.v1", result_payload
        ),
    )
