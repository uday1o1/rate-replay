"""Canonical integer tariff IR and compilation report models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ratereplay_tariffs.schema import (
    ChargeComponentKey,
    DateRange,
    FrozenModel,
    RoundingOperator,
    RuleApplicability,
    SourceLink,
)


class IRRuleBase(FrozenModel):
    rule_id: str
    effective_range: DateRange
    applicability: RuleApplicability
    source: SourceLink
    rounding: RoundingOperator
    charge_component_key: ChargeComponentKey


class IRBaselineAllowance(IRRuleBase):
    operator: Literal["BASELINE_ALLOWANCE"]
    daily_allowance_wh: int


class IREnergyTier(FrozenModel):
    upper_bound_operator: Literal["BASELINE_ALLOWANCE", "UNBOUNDED"]
    upper_bound_numerator: int
    upper_bound_denominator: int
    rate_microdollars_per_kwh: int


class IRTieredEnergyCharge(IRRuleBase):
    operator: Literal["TIER_ALLOCATE_AND_MULTIPLY_RATIONAL"]
    line_item_key: str
    tiers: tuple[IREnergyTier, ...]


class IRFixedDailyCharge(IRRuleBase):
    operator: Literal["MULTIPLY_DAYS_BY_INTEGER_RATE"]
    line_item_key: str
    rate_microdollars_per_day: int


class IRFixedMonthlyCharge(IRRuleBase):
    operator: Literal["APPLICABILITY_GATED_INTEGER_AMOUNT"]
    line_item_key: str
    amount_microdollars: int


class IRExplicitUnsupportedCharge(IRRuleBase):
    operator: Literal["EMIT_UNSUPPORTED_PLACEHOLDER"]
    line_item_key: str
    reason_code: str


CompiledRule = Annotated[
    IRBaselineAllowance
    | IRTieredEnergyCharge
    | IRFixedDailyCharge
    | IRFixedMonthlyCharge
    | IRExplicitUnsupportedCharge,
    Field(discriminator="operator"),
]


class CanonicalChargeIR(FrozenModel):
    ir_version: Literal["compiled-charge-ir-v1"]
    tariff_version_id: str
    maximum_energy_wh: int
    maximum_billing_days: int
    operators: tuple[CompiledRule, ...]


class CoverageReport(FrozenModel):
    service_windows: tuple[DateRange, ...]
    complete_component_keys: tuple[str, ...]
    active_component_count_by_key: tuple[int, ...]


class SourceCoverage(FrozenModel):
    source_id: str
    source_sha256: str
    source_url: str
    linked_rule_ids: tuple[str, ...]


class GoldenCoverage(FrozenModel):
    golden_case_ids: tuple[str, ...]
    rule_case_ids: dict[str, tuple[str, ...]]


class CompilationReports(FrozenModel):
    normalized_ast_sha256: str
    eligibility_predicate_id: str
    component_vector: CoverageReport
    source_coverage: tuple[SourceCoverage, ...]
    golden_coverage: GoldenCoverage
    solver_lowering_supported_operators: tuple[str, ...]
    solver_lowering_unsupported_reasons: tuple[str, ...]


class CompilationBundle(FrozenModel):
    bundle_version: Literal["tariff-compilation-bundle-v1"]
    normalized_ast: dict[str, object]
    ir: CanonicalChargeIR
    reports: CompilationReports
    compiler_content_sha256: str
