"""Strict immutable tariff-language models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Currency = Literal["USD"]
EligibilityStatus = Literal["ELIGIBLE", "INELIGIBLE", "UNKNOWN"]
IncomeTier = Literal["TIER_1", "TIER_2", "TIER_3", "TIER_4"]
BaselineTerritory = Literal["P", "Q", "R", "S", "T", "V", "W", "X", "Y", "Z"]
BaselineQuantityCode = Literal["BASIC", "ALL_ELECTRIC"]
QualifyingTechnology = Literal["EV", "HEAT_PUMP", "ELECTRIC_WATER_HEAT"]
RoundingOperator = Literal["EXACT", "HALF_UP_CENT_AT_LINE_ITEM"]
ChargeComponentKey = Literal[
    "baseline_allowance",
    "bundled_energy",
    "baseline_adjustment",
    "base_services_charge",
    "california_climate_credit",
    "minimum_bill_adjustment",
    "explicit_unsupported",
]


class FrozenModel(BaseModel):
    """Reject unknown or coerced fields and prohibit mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DateRange(FrozenModel):
    start: date
    end: date | None

    @model_validator(mode="after")
    def validate_nonempty(self) -> DateRange:
        if self.end is not None and self.end <= self.start:
            raise ValueError("date range must be nonempty and half-open")
        return self

    def contains(self, value: date) -> bool:
        return value >= self.start and (self.end is None or value < self.end)

    def covers(self, other: DateRange) -> bool:
        if self.start > other.start:
            return False
        if self.end is None:
            return True
        return other.end is not None and self.end >= other.end


class SourceLink(FrozenModel):
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sheet_ids: tuple[str, ...] = Field(min_length=1)


class TariffComponentVersion(FrozenModel):
    component_version_id: str = Field(min_length=1)
    component_key: str = Field(min_length=1)
    effective_range: DateRange
    precedence: int = Field(ge=0)
    source: SourceLink
    extracted_rule_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rule_id_set(self) -> TariffComponentVersion:
        if len(self.extracted_rule_ids) != len(set(self.extracted_rule_ids)):
            raise ValueError("component extracted rule identifiers must be unique")
        return self


class AccountFacts(FrozenModel):
    schema_version: Literal["account-facts-v1"]
    service_window: DateRange
    service_provider: Literal["PG&E"]
    service_mode: Literal["BUNDLED"]
    meter_count: int = Field(ge=1)
    primary_meter_only: bool
    income_tier: IncomeTier
    care_enrolled: bool
    fera_enrolled: bool
    medical_baseline: bool
    cca_service: bool
    direct_access_service: bool
    active_bill_protection: bool
    solar_or_export: bool
    baseline_territory: BaselineTerritory
    baseline_quantity_code: BaselineQuantityCode
    qualifying_technologies: tuple[QualifyingTechnology, ...] = ()
    user_attested_at: datetime

    @model_validator(mode="after")
    def validate_technologies(self) -> AccountFacts:
        if tuple(sorted(self.qualifying_technologies)) != self.qualifying_technologies:
            raise ValueError("qualifying technologies must be unique and sorted")
        return self


class EligibilityPredicate(FrozenModel):
    predicate_id: str = Field(min_length=1)
    predicate_version: str = Field(min_length=1)
    supported_income_tiers: tuple[IncomeTier, ...] = Field(min_length=1)
    required_service_provider: Literal["PG&E"]
    required_service_mode: Literal["BUNDLED"]
    required_meter_count: Literal[1]
    requires_primary_meter_only: Literal[True]
    requires_care_enrolled: Literal[False]
    requires_fera_enrolled: Literal[False]
    requires_medical_baseline: Literal[False]
    requires_cca_service: Literal[False]
    requires_direct_access_service: Literal[False]
    requires_active_bill_protection: Literal[False]
    requires_solar_or_export: Literal[False]
    required_qualifying_technology: QualifyingTechnology | None = None
    source_rule_ids: tuple[str, ...] = Field(min_length=1)


class RuleApplicability(FrozenModel):
    income_tiers: tuple[IncomeTier, ...] = ()
    baseline_territories: tuple[BaselineTerritory, ...] = ()
    baseline_quantity_codes: tuple[BaselineQuantityCode, ...] = ()
    bill_cycle_months: tuple[Annotated[int, Field(ge=1, le=12)], ...] = ()

    @model_validator(mode="after")
    def validate_unique_sorted(self) -> RuleApplicability:
        for values in (
            self.income_tiers,
            self.baseline_territories,
            self.baseline_quantity_codes,
            self.bill_cycle_months,
        ):
            if tuple(sorted(values)) != values or len(values) != len(set(values)):
                raise ValueError("applicability values must be unique and sorted")
        return self


class RuleBase(FrozenModel):
    rule_id: str = Field(min_length=1)
    effective_range: DateRange
    applicability: RuleApplicability
    source: SourceLink
    rounding: RoundingOperator
    charge_component_key: ChargeComponentKey


class BaselineAllowance(RuleBase):
    rule_type: Literal["BaselineAllowance"]
    daily_allowance_wh: int = Field(gt=0)
    unit: Literal["Wh/day"]


class EnergyTier(FrozenModel):
    upper_bound_kind: Literal["BASELINE_ALLOWANCE", "UNBOUNDED"]
    upper_bound_numerator: int = Field(gt=0)
    upper_bound_denominator: int = Field(gt=0)
    rate_microdollars_per_kwh: int = Field(ge=0)


class TieredEnergyCharge(RuleBase):
    rule_type: Literal["TieredEnergyCharge"]
    line_item_key: str = Field(min_length=1)
    quantity_unit: Literal["Wh"]
    rate_unit: Literal["microdollars/kWh"]
    tiers: tuple[EnergyTier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tiers(self) -> TieredEnergyCharge:
        if self.tiers[-1].upper_bound_kind != "UNBOUNDED":
            raise ValueError("final energy tier must be unbounded")
        if any(tier.upper_bound_kind == "UNBOUNDED" for tier in self.tiers[:-1]):
            raise ValueError("only the final energy tier may be unbounded")
        return self


class FixedDailyCharge(RuleBase):
    rule_type: Literal["FixedDailyCharge"]
    line_item_key: str = Field(min_length=1)
    rate_microdollars_per_day: int
    rate_unit: Literal["microdollars/day"]


class FixedMonthlyCharge(RuleBase):
    rule_type: Literal["FixedMonthlyCharge"]
    line_item_key: str = Field(min_length=1)
    amount_microdollars: int
    rate_unit: Literal["microdollars/bill_cycle"]


class ExplicitUnsupportedCharge(RuleBase):
    rule_type: Literal["ExplicitUnsupportedCharge"]
    line_item_key: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)


TariffRule = Annotated[
    BaselineAllowance
    | TieredEnergyCharge
    | FixedDailyCharge
    | FixedMonthlyCharge
    | ExplicitUnsupportedCharge,
    Field(discriminator="rule_type"),
]


class TariffVersion(FrozenModel):
    schema_version: Literal["tariff-schema-v1"]
    compiler_version: Literal["tariff-compiler-v1"]
    tariff_version_id: str = Field(min_length=1)
    utility: Literal["PG&E"]
    plan_code: str = Field(min_length=1)
    admitted_service_windows: tuple[DateRange, ...] = Field(min_length=1)
    component_versions: tuple[TariffComponentVersion, ...] = Field(min_length=1)
    timezone: Literal["America/Los_Angeles"]
    currency: Currency
    eligibility_predicate: EligibilityPredicate
    eligibility_questions: tuple[str, ...]
    comparison_component_keys: tuple[ChargeComponentKey, ...] = Field(min_length=1)
    optimization_capability: Literal["SUPPORTED", "UNSUPPORTED_WITH_REASON"]
    optimization_unsupported_reason: str | None
    charge_rules: tuple[TariffRule, ...] = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)
    source_hashes: tuple[str, ...] = Field(min_length=1)
    golden_case_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_collections(self) -> TariffVersion:
        unique_collections = (
            self.source_ids,
            self.source_hashes,
            self.golden_case_ids,
            tuple(rule.rule_id for rule in self.charge_rules),
            tuple(component.component_version_id for component in self.component_versions),
        )
        if any(len(values) != len(set(values)) for values in unique_collections):
            raise ValueError("tariff identifiers and lock lists must be unique")
        if tuple(sorted(self.source_ids)) != self.source_ids:
            raise ValueError("source identifiers must be sorted")
        if tuple(sorted(self.source_hashes)) != self.source_hashes:
            raise ValueError("source hashes must be sorted")
        if tuple(sorted(self.golden_case_ids)) != self.golden_case_ids:
            raise ValueError("golden case identifiers must be sorted")
        if self.optimization_capability == "SUPPORTED" and self.optimization_unsupported_reason:
            raise ValueError("supported optimization cannot carry an unsupported reason")
        if (
            self.optimization_capability == "UNSUPPORTED_WITH_REASON"
            and not self.optimization_unsupported_reason
        ):
            raise ValueError("unsupported optimization requires a reason")
        return self
