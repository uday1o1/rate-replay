"""Exact reference evaluator for compiled tariff IR."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Literal, cast
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from ratereplay_tariffs.compiled import (
    CompilationBundle,
    IRBaselineAllowance,
    IRExplicitUnsupportedCharge,
    IRFixedDailyCharge,
    IRFixedMonthlyCharge,
    IRTieredEnergyCharge,
    IRTimeOfUseEnergyCharge,
    IRTimeOfUseSchedule,
)
from ratereplay_tariffs.hashing import canonical_content_sha256, canonical_json_bytes
from ratereplay_tariffs.ir import round_half_up_cents
from ratereplay_tariffs.schema import (
    AccountFacts,
    ChargeComponentKey,
    DatedEligibilityFacts,
    DateRange,
    EligibilityPredicate,
    EligibilityPredicateV2,
    EligibilityStatus,
    FrozenModel,
    RoundingOperator,
    RuleApplicability,
)


class ReplayError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UserUnsupportedLine(FrozenModel):
    line_item_key: str = Field(min_length=1)
    description: str = Field(min_length=1)
    amount_cents: int


class ReplayRequestBase(FrozenModel):
    profile_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_facts: AccountFacts
    energy_wh: int = Field(ge=0)
    current_bill_total_cents: int | None = None
    user_unsupported_lines: tuple[UserUnsupportedLine, ...] = ()

    @field_validator("user_unsupported_lines")
    @classmethod
    def canonicalize_unsupported_lines(
        cls, values: tuple[UserUnsupportedLine, ...]
    ) -> tuple[UserUnsupportedLine, ...]:
        keys = tuple(item.line_item_key for item in values)
        if len(keys) != len(set(keys)):
            raise ValueError("user unsupported line keys must be unique")
        return tuple(sorted(values, key=lambda item: item.line_item_key))

    def validate_reconciliation_inputs(self) -> None:
        if self.current_bill_total_cents is None and self.user_unsupported_lines:
            raise ReplayError(
                "UNSUPPORTED_LINES_REQUIRE_BILL_TOTAL",
                "User-entered unsupported lines require a current-bill total.",
            )


class ReplayRequest(ReplayRequestBase):
    request_version: Literal["e1-replay-request-v1"]


class ReplayInterval(FrozenModel):
    start_utc_ns: int = Field(ge=0)
    duration_seconds: int = Field(gt=0, le=3600)
    energy_wh: int = Field(ge=0)


class IntervalReplayRequest(ReplayRequestBase):
    request_version: Literal["interval-replay-request-v1"]
    intervals: tuple[ReplayInterval, ...] = Field(min_length=1)
    dated_eligibility_facts: DatedEligibilityFacts | None = None

    @model_validator(mode="after")
    def validate_intervals(self) -> IntervalReplayRequest:
        if sum(interval.energy_wh for interval in self.intervals) != self.energy_wh:
            raise ValueError("interval energy must equal total energy")
        previous_end_ns = -1
        for interval in self.intervals:
            if interval.start_utc_ns % 1_000_000_000:
                raise ValueError("interval starts must align to whole UTC seconds")
            if interval.start_utc_ns < previous_end_ns:
                raise ValueError("replay intervals must be sorted and disjoint")
            previous_end_ns = interval.start_utc_ns + interval.duration_seconds * 1_000_000_000
        return self


class EligibilityResult(FrozenModel):
    status: EligibilityStatus
    reason_codes: tuple[str, ...]
    predicate_id: str
    predicate_version: str
    source_rule_ids: tuple[str, ...]
    account_facts_sha256: str


class ChargeLineItem(FrozenModel):
    rule_id: str
    tariff_version_id: str
    source_id: str
    source_sheet_ids: tuple[str, ...]
    line_item_key: str
    charge_component_key: ChargeComponentKey
    quantity_numerator: int
    quantity_denominator: int = 1
    quantity_unit: str
    rate_numerator_microdollars: int
    rate_denominator: int = 1
    rate_unit: str
    pre_round_microdollars_numerator: int
    pre_round_microdollars_denominator: int
    rounded_cents: int
    contributing_service_window: DateRange
    explanation_key: str
    rounding_operator: RoundingOperator
    rounding_boundary: Literal["LINE_ITEM"]


class UnsupportedPlaceholder(FrozenModel):
    rule_id: str
    line_item_key: str
    source_id: str
    reason_code: str
    amount_cents: None = None


class ReconciliationPolicy(FrozenModel):
    policy_version: Literal["current-bill-reconciliation-v1"] = "current-bill-reconciliation-v1"
    review_tolerance_cents: int = 100
    warning_threshold_cents: int = 500


class ReconciliationResult(FrozenModel):
    supported_calculated_cents: int
    user_unsupported_cents: int
    unexplained_residual_cents: int
    entered_bill_total_cents: int
    classification: Literal["EXACT", "WITHIN_REVIEW_TOLERANCE", "REVIEW_REQUIRED"]
    input_sha256: str
    policy_sha256: str


class CalculationManifest(FrozenModel):
    manifest_version: Literal["e1-calculation-manifest-v1"]
    calculation_time_mode: Literal["HISTORICAL_REPLAY"]
    profile_content_sha256: str
    tariff_compiler_content_sha256: str
    tariff_ir_version: str
    tariff_version_id: str
    account_facts_sha256: str
    replay_input_sha256: str
    reconciliation_input_sha256: str | None
    reconciliation_policy_sha256: str | None
    baseline_allowance_wh: int
    billing_days: int
    bill_cycle_month: int
    calculation_sha256: str


class IntervalCalculationManifest(FrozenModel):
    manifest_version: Literal["historical-replay-manifest-v2"]
    calculation_time_mode: Literal["HISTORICAL_REPLAY"]
    profile_content_sha256: str
    tariff_compiler_content_sha256: str
    tariff_ir_version: str
    tariff_version_id: str
    account_facts_sha256: str
    replay_input_sha256: str
    reconciliation_input_sha256: str | None
    reconciliation_policy_sha256: str | None
    baseline_allowance_wh: int
    billing_days: int
    bill_cycle_month: int
    period_energy_wh: dict[str, int]
    calculation_sha256: str


class ReplayResult(FrozenModel):
    result_version: Literal["e1-replay-result-v1", "historical-replay-result-v2"]
    eligibility: EligibilityResult
    supported_calculated_cents: int
    line_items: tuple[ChargeLineItem, ...]
    tariff_unsupported_placeholders: tuple[UnsupportedPlaceholder, ...]
    user_unsupported_lines: tuple[UserUnsupportedLine, ...]
    reconciliation: ReconciliationResult | None
    provenance_sources: tuple[dict[str, object], ...]
    manifest: CalculationManifest | IntervalCalculationManifest
    result_sha256: str


def _predicate(bundle: CompilationBundle) -> EligibilityPredicate | EligibilityPredicateV2:
    value = bundle.normalized_ast.get("eligibility_predicate")
    try:
        if isinstance(value, dict) and value.get("predicate_version") == "eligibility-predicate-v2":
            return EligibilityPredicateV2.model_validate_json(canonical_json_bytes(value))
        return EligibilityPredicate.model_validate_json(canonical_json_bytes(value))
    except ValueError as error:
        raise ReplayError(
            "COMPILED_ELIGIBILITY_INVALID", "Compiled eligibility is invalid"
        ) from error


def _facts_hash(
    facts: AccountFacts,
    dated_facts: DatedEligibilityFacts | None,
    *,
    predicate_version: str,
) -> str:
    if predicate_version == "eligibility-predicate-v1":
        return canonical_content_sha256(
            b"RateReplay.AccountFacts.v1", facts.model_dump(mode="json")
        )
    return canonical_content_sha256(
        b"RateReplay.EligibilityFacts.v2",
        {
            "account_facts": facts.model_dump(mode="json"),
            "dated_eligibility_facts": (
                dated_facts.model_dump(mode="json") if dated_facts is not None else None
            ),
        },
    )


def evaluate_eligibility(
    bundle: CompilationBundle,
    facts: AccountFacts,
    dated_facts: DatedEligibilityFacts | None = None,
) -> EligibilityResult:
    predicate = _predicate(bundle)
    unknown: list[str] = []
    ineligible: list[str] = []
    service_windows = bundle.reports.component_vector.service_windows
    if facts.service_window not in service_windows:
        ineligible.append("SERVICE_WINDOW_NOT_ADMITTED")
    if facts.income_tier not in predicate.supported_income_tiers:
        unknown.append("UNSUPPORTED_INCOME_TIER")
    requirements: tuple[tuple[bool, str], ...] = (
        (
            facts.service_provider == predicate.required_service_provider,
            "SERVICE_PROVIDER_MISMATCH",
        ),
        (facts.service_mode == predicate.required_service_mode, "SERVICE_MODE_MISMATCH"),
        (facts.meter_count == predicate.required_meter_count, "METER_COUNT_MISMATCH"),
        (
            facts.primary_meter_only == predicate.requires_primary_meter_only,
            "PRIMARY_METER_REQUIREMENT_MISMATCH",
        ),
        (facts.care_enrolled == predicate.requires_care_enrolled, "CARE_STATUS_MISMATCH"),
        (facts.fera_enrolled == predicate.requires_fera_enrolled, "FERA_STATUS_MISMATCH"),
        (
            facts.medical_baseline == predicate.requires_medical_baseline,
            "MEDICAL_BASELINE_STATUS_MISMATCH",
        ),
        (facts.cca_service == predicate.requires_cca_service, "CCA_STATUS_MISMATCH"),
        (
            facts.direct_access_service == predicate.requires_direct_access_service,
            "DIRECT_ACCESS_STATUS_MISMATCH",
        ),
        (
            facts.active_bill_protection == predicate.requires_active_bill_protection,
            "BILL_PROTECTION_STATUS_MISMATCH",
        ),
        (
            facts.solar_or_export == predicate.requires_solar_or_export,
            "SOLAR_EXPORT_STATUS_MISMATCH",
        ),
    )
    ineligible.extend(code for matches, code in requirements if not matches)
    if predicate.required_qualifying_technology is not None:
        if facts.qualifying_technologies is None:
            unknown.append("QUALIFYING_TECHNOLOGY_FACT_MISSING")
        elif predicate.required_qualifying_technology not in facts.qualifying_technologies:
            ineligible.append("QUALIFYING_TECHNOLOGY_MISSING")
    if isinstance(predicate, EligibilityPredicateV2):
        if predicate.required_any_qualifying_technologies:
            if facts.qualifying_technologies is None:
                unknown.append("QUALIFYING_TECHNOLOGY_FACT_MISSING")
            elif not set(predicate.required_any_qualifying_technologies).intersection(
                facts.qualifying_technologies
            ):
                ineligible.append("QUALIFYING_TECHNOLOGY_MISSING")
        if predicate.requires_dated_technology_attestation:
            if dated_facts is None or dated_facts.facts_as_of is None:
                unknown.append("DATED_TECHNOLOGY_FACT_MISSING")
            elif dated_facts.facts_as_of != facts.service_window.start:
                unknown.append("DATED_TECHNOLOGY_FACT_OUTSIDE_WINDOW")
        if predicate.requires_ev_registration_at_premises:
            if dated_facts is None or dated_facts.ev_registered_and_charged_at_premises is None:
                unknown.append("EV_REGISTRATION_FACT_MISSING")
            elif not dated_facts.ev_registered_and_charged_at_premises:
                ineligible.append("EV_REGISTRATION_REQUIREMENT_MISMATCH")
        if predicate.requires_whole_house_metering:
            if dated_facts is None or dated_facts.whole_house_metering is None:
                unknown.append("WHOLE_HOUSE_METERING_FACT_MISSING")
            elif not dated_facts.whole_house_metering:
                ineligible.append("WHOLE_HOUSE_METERING_REQUIREMENT_MISMATCH")
        if predicate.maximum_annual_baseline_ratio_numerator is not None:
            if (
                dated_facts is None
                or dated_facts.annual_usage_wh is None
                or dated_facts.annual_baseline_allowance_wh is None
                or dated_facts.annual_usage_period is None
            ):
                unknown.append("ANNUAL_BASELINE_FACT_MISSING")
            else:
                period = dated_facts.annual_usage_period
                expected_start = facts.service_window.start.replace(
                    year=facts.service_window.start.year - 1
                )
                if period.start != expected_start or period.end != facts.service_window.start:
                    unknown.append("ANNUAL_USAGE_PERIOD_MISMATCH")
                else:
                    numerator = predicate.maximum_annual_baseline_ratio_numerator
                    denominator = predicate.maximum_annual_baseline_ratio_denominator
                    if denominator is None:
                        raise ReplayError(
                            "COMPILED_ELIGIBILITY_INVALID",
                            "Compiled annual baseline denominator is missing.",
                        )
                    if (
                        dated_facts.annual_usage_wh * denominator
                        > dated_facts.annual_baseline_allowance_wh * numerator
                    ):
                        ineligible.append("ANNUAL_BASELINE_LIMIT_EXCEEDED")
    baseline_rules = tuple(
        rule for rule in bundle.ir.operators if isinstance(rule, IRBaselineAllowance)
    )
    bill_cycle_month = facts.service_window.end.month if facts.service_window.end else 0
    if baseline_rules and not any(
        _applies(rule.applicability, facts, bill_cycle_month) for rule in baseline_rules
    ):
        unknown.append("UNSUPPORTED_BASELINE_CONFIGURATION")
    if ineligible:
        status: EligibilityStatus = "INELIGIBLE"
        reasons = tuple(sorted(set(ineligible)))
    elif unknown:
        status = "UNKNOWN"
        reasons = tuple(sorted(set(unknown)))
    else:
        status = "ELIGIBLE"
        reasons = ()
    return EligibilityResult(
        status=status,
        reason_codes=reasons,
        predicate_id=predicate.predicate_id,
        predicate_version=predicate.predicate_version,
        source_rule_ids=predicate.source_rule_ids,
        account_facts_sha256=_facts_hash(
            facts, dated_facts, predicate_version=predicate.predicate_version
        ),
    )


def _applies(applicability: RuleApplicability, facts: AccountFacts, bill_cycle_month: int) -> bool:
    return (
        (not applicability.income_tiers or facts.income_tier in applicability.income_tiers)
        and (
            not applicability.baseline_territories
            or facts.baseline_territory in applicability.baseline_territories
        )
        and (
            not applicability.baseline_quantity_codes
            or facts.baseline_quantity_code in applicability.baseline_quantity_codes
        )
        and (
            not applicability.bill_cycle_months
            or bill_cycle_month in applicability.bill_cycle_months
        )
    )


def _line(
    *,
    bundle: CompilationBundle,
    rule: (
        IRTieredEnergyCharge | IRTimeOfUseEnergyCharge | IRFixedDailyCharge | IRFixedMonthlyCharge
    ),
    line_item_key: str,
    quantity: int,
    quantity_unit: str,
    rate: int,
    rate_unit: str,
    raw_microdollars: Fraction,
    service_window: DateRange,
) -> ChargeLineItem:
    return ChargeLineItem(
        rule_id=rule.rule_id,
        tariff_version_id=bundle.ir.tariff_version_id,
        source_id=rule.source.source_id,
        source_sheet_ids=rule.source.source_sheet_ids,
        line_item_key=line_item_key,
        charge_component_key=rule.charge_component_key,
        quantity_numerator=quantity,
        quantity_unit=quantity_unit,
        rate_numerator_microdollars=rate,
        rate_unit=rate_unit,
        pre_round_microdollars_numerator=raw_microdollars.numerator,
        pre_round_microdollars_denominator=raw_microdollars.denominator,
        rounded_cents=round_half_up_cents(raw_microdollars),
        contributing_service_window=service_window,
        explanation_key=f"tariff.{rule.rule_id.lower()}",
        rounding_operator=rule.rounding,
        rounding_boundary="LINE_ITEM",
    )


def _baseline_allowance(
    bundle: CompilationBundle,
    facts: AccountFacts,
    billing_days: int,
    bill_cycle_month: int,
) -> int:
    rules = tuple(
        rule
        for rule in bundle.ir.operators
        if isinstance(rule, IRBaselineAllowance)
        and _applies(rule.applicability, facts, bill_cycle_month)
    )
    all_baseline_rules = tuple(
        rule for rule in bundle.ir.operators if isinstance(rule, IRBaselineAllowance)
    )
    if not all_baseline_rules:
        return 0
    if len(rules) != 1:
        raise ReplayError("BASELINE_RULE_AMBIGUITY", "Exactly one baseline allowance must apply.")
    return rules[0].daily_allowance_wh * billing_days


def _time_schedule(bundle: CompilationBundle, rule_id: str) -> IRTimeOfUseSchedule:
    schedules = tuple(
        operator
        for operator in bundle.ir.operators
        if isinstance(operator, IRTimeOfUseSchedule) and operator.rule_id == rule_id
    )
    if len(schedules) != 1:
        raise ReplayError("TOU_SCHEDULE_AMBIGUITY", "Exactly one referenced schedule must exist.")
    return schedules[0]


def _classify_local_period(schedule: IRTimeOfUseSchedule, local_start: datetime) -> str:
    minute = local_start.hour * 60 + local_start.minute
    holiday_dates = frozenset(schedule.holiday_dates)
    for window in schedule.windows:
        day_matches = window.day_selector == "EVERY_DAY" or (
            window.day_selector == "NONHOLIDAY_WEEKDAY"
            and local_start.weekday() < 5
            and local_start.date().isoformat() not in holiday_dates
        )
        if day_matches and window.start_minute_inclusive <= minute < window.end_minute_exclusive:
            return window.period
    return schedule.default_period


def _period_energy(
    request: ReplayRequest | IntervalReplayRequest,
    schedule: IRTimeOfUseSchedule,
) -> dict[str, int]:
    if not isinstance(request, IntervalReplayRequest):
        raise ReplayError(
            "INTERVAL_DATA_REQUIRED", "Time-of-use replay requires canonical interval data."
        )
    timezone = ZoneInfo(schedule.timezone)
    totals: dict[str, int] = {}
    for interval in request.intervals:
        start_seconds = interval.start_utc_ns // 1_000_000_000
        start_utc = datetime.fromtimestamp(start_seconds, tz=UTC)
        end_utc = start_utc + timedelta(seconds=interval.duration_seconds)
        local_start = start_utc.astimezone(timezone)
        local_final_instant = (end_utc - timedelta(microseconds=1)).astimezone(timezone)
        start_period = _classify_local_period(schedule, local_start)
        end_period = _classify_local_period(schedule, local_final_instant)
        if start_period != end_period:
            raise ReplayError(
                "INTERVAL_CROSSES_TARIFF_BOUNDARY",
                "An interval crosses a time-of-use boundary and cannot be apportioned.",
            )
        service_window = request.account_facts.service_window
        if not service_window.contains(local_start.date()) or not service_window.contains(
            local_final_instant.date()
        ):
            raise ReplayError(
                "INTERVAL_OUTSIDE_SERVICE_WINDOW", "An interval is outside the billing period."
            )
        totals[start_period] = totals.get(start_period, 0) + interval.energy_wh
    return totals


def _reconcile(
    request: ReplayRequest | IntervalReplayRequest,
    supported_cents: int,
    policy: ReconciliationPolicy,
) -> ReconciliationResult | None:
    if request.current_bill_total_cents is None:
        return None
    input_payload = {
        "entered_bill_total_cents": request.current_bill_total_cents,
        "user_unsupported_lines": [
            item.model_dump(mode="json") for item in request.user_unsupported_lines
        ],
    }
    input_hash = canonical_content_sha256(b"RateReplay.ReconciliationInput.v1", input_payload)
    policy_hash = canonical_content_sha256(
        b"RateReplay.ReconciliationPolicy.v1", policy.model_dump(mode="json")
    )
    unsupported_cents = sum(item.amount_cents for item in request.user_unsupported_lines)
    residual = request.current_bill_total_cents - supported_cents - unsupported_cents
    absolute_residual = abs(residual)
    if residual == 0:
        classification: Literal["EXACT", "WITHIN_REVIEW_TOLERANCE", "REVIEW_REQUIRED"] = "EXACT"
    elif absolute_residual <= policy.review_tolerance_cents:
        classification = "WITHIN_REVIEW_TOLERANCE"
    else:
        classification = "REVIEW_REQUIRED"
    return ReconciliationResult(
        supported_calculated_cents=supported_cents,
        user_unsupported_cents=unsupported_cents,
        unexplained_residual_cents=residual,
        entered_bill_total_cents=request.current_bill_total_cents,
        classification=classification,
        input_sha256=input_hash,
        policy_sha256=policy_hash,
    )


def replay_compiled_tariff(
    bundle: CompilationBundle,
    request: ReplayRequest | IntervalReplayRequest,
    *,
    policy: ReconciliationPolicy | None = None,
) -> ReplayResult:
    """Evaluate compiled IR using only exact integer and rational operations."""

    request.validate_reconciliation_inputs()
    dated_facts = (
        request.dated_eligibility_facts if isinstance(request, IntervalReplayRequest) else None
    )
    eligibility = evaluate_eligibility(bundle, request.account_facts, dated_facts)
    if eligibility.status != "ELIGIBLE":
        raise ReplayError(
            f"TARIFF_{eligibility.status}",
            f"Tariff replay requires ELIGIBLE facts: {eligibility.reason_codes}",
        )
    service_window = request.account_facts.service_window
    if service_window.end is None:
        raise ReplayError("UNBOUNDED_BILLING_PERIOD", "Billing period must be bounded.")
    billing_days = (service_window.end - service_window.start).days
    if billing_days <= 0 or billing_days > bundle.ir.maximum_billing_days:
        raise ReplayError("INVALID_BILLING_PERIOD", "Billing period is outside compiled bounds.")
    if request.energy_wh > bundle.ir.maximum_energy_wh:
        raise ReplayError("ENERGY_BOUND_EXCEEDED", "Energy exceeds the compiled int64 bound.")
    bill_cycle_month = service_window.end.month
    baseline_wh = _baseline_allowance(bundle, request.account_facts, billing_days, bill_cycle_month)
    lines: list[ChargeLineItem] = []
    placeholders: list[UnsupportedPlaceholder] = []
    period_energy_by_schedule: dict[str, dict[str, int]] = {}
    for operator in bundle.ir.operators:
        if not _applies(operator.applicability, request.account_facts, bill_cycle_month):
            continue
        if isinstance(operator, (IRBaselineAllowance, IRTimeOfUseSchedule)):
            continue
        if isinstance(operator, IRTieredEnergyCharge):
            remaining = request.energy_wh
            lower_bound = 0
            for index, tier in enumerate(operator.tiers, start=1):
                if tier.upper_bound_operator == "BASELINE_ALLOWANCE":
                    numerator = baseline_wh * tier.upper_bound_numerator
                    if numerator % tier.upper_bound_denominator:
                        raise ReplayError(
                            "NONINTEGRAL_TIER_BOUND", "Tier bound is not an exact watt-hour value."
                        )
                    upper_bound = numerator // tier.upper_bound_denominator
                    quantity = min(remaining, upper_bound - lower_bound)
                else:
                    upper_bound = None
                    quantity = remaining
                if quantity < 0:
                    raise ReplayError("INVALID_TIER_BOUND", "Tier bounds are not increasing.")
                if quantity:
                    raw = Fraction(
                        quantity * tier.rate_microdollars_per_kwh,
                        1000,
                    )
                    lines.append(
                        _line(
                            bundle=bundle,
                            rule=operator,
                            line_item_key=f"{operator.line_item_key}.tier_{index}",
                            quantity=quantity,
                            quantity_unit="Wh",
                            rate=tier.rate_microdollars_per_kwh,
                            rate_unit="microdollars/kWh",
                            raw_microdollars=raw,
                            service_window=service_window,
                        )
                    )
                    remaining -= quantity
                if upper_bound is not None:
                    lower_bound = upper_bound
                if remaining == 0:
                    break
            if remaining:
                raise ReplayError("UNCOVERED_TIER", "Energy remains after tier allocation.")
        elif isinstance(operator, IRTimeOfUseEnergyCharge):
            schedule = _time_schedule(bundle, operator.schedule_rule_id)
            period_energy = period_energy_by_schedule.setdefault(
                schedule.rule_id, _period_energy(request, schedule)
            )
            for period_rate in operator.period_rates:
                quantity = period_energy.get(period_rate.period, 0)
                if not quantity:
                    continue
                raw = Fraction(
                    quantity * period_rate.rate_microdollars_per_kwh,
                    1000,
                )
                lines.append(
                    _line(
                        bundle=bundle,
                        rule=operator,
                        line_item_key=(f"{operator.line_item_key}.{period_rate.period.lower()}"),
                        quantity=quantity,
                        quantity_unit="Wh",
                        rate=period_rate.rate_microdollars_per_kwh,
                        rate_unit="microdollars/kWh",
                        raw_microdollars=raw,
                        service_window=service_window,
                    )
                )
            if operator.baseline_credit_microdollars_per_kwh is not None:
                baseline_quantity = min(request.energy_wh, baseline_wh)
                raw = Fraction(
                    baseline_quantity * operator.baseline_credit_microdollars_per_kwh,
                    1000,
                )
                lines.append(
                    _line(
                        bundle=bundle,
                        rule=operator,
                        line_item_key=f"{operator.line_item_key}.baseline_credit",
                        quantity=baseline_quantity,
                        quantity_unit="Wh",
                        rate=operator.baseline_credit_microdollars_per_kwh,
                        rate_unit="microdollars/kWh",
                        raw_microdollars=raw,
                        service_window=service_window,
                    )
                )
        elif isinstance(operator, IRFixedDailyCharge):
            raw = Fraction(billing_days * operator.rate_microdollars_per_day)
            lines.append(
                _line(
                    bundle=bundle,
                    rule=operator,
                    line_item_key=operator.line_item_key,
                    quantity=billing_days,
                    quantity_unit="days",
                    rate=operator.rate_microdollars_per_day,
                    rate_unit="microdollars/day",
                    raw_microdollars=raw,
                    service_window=service_window,
                )
            )
        elif isinstance(operator, IRFixedMonthlyCharge):
            raw = Fraction(operator.amount_microdollars)
            lines.append(
                _line(
                    bundle=bundle,
                    rule=operator,
                    line_item_key=operator.line_item_key,
                    quantity=1,
                    quantity_unit="bill_cycle",
                    rate=operator.amount_microdollars,
                    rate_unit="microdollars/bill_cycle",
                    raw_microdollars=raw,
                    service_window=service_window,
                )
            )
        elif isinstance(operator, IRExplicitUnsupportedCharge):
            placeholders.append(
                UnsupportedPlaceholder(
                    rule_id=operator.rule_id,
                    line_item_key=operator.line_item_key,
                    source_id=operator.source.source_id,
                    reason_code=operator.reason_code,
                )
            )
        else:
            raise ReplayError("UNSUPPORTED_IR_OPERATOR", "Compiled IR operator is unsupported.")
    supported_cents = sum(line.rounded_cents for line in lines)
    resolved_policy = policy or ReconciliationPolicy()
    reconciliation = _reconcile(request, supported_cents, resolved_policy)
    replay_input_payload: dict[str, object] = {
        "profile_content_sha256": request.profile_content_sha256,
        "energy_wh": request.energy_wh,
        "service_window": service_window.model_dump(mode="json"),
    }
    replay_input_domain = b"RateReplay.ReplayInput.v1"
    if isinstance(request, IntervalReplayRequest):
        replay_input_domain = b"RateReplay.IntervalReplayInput.v2"
        replay_input_payload["intervals"] = [
            interval.model_dump(mode="json") for interval in request.intervals
        ]
        replay_input_payload["dated_eligibility_facts"] = (
            request.dated_eligibility_facts.model_dump(mode="json")
            if request.dated_eligibility_facts is not None
            else None
        )
    replay_input_hash = canonical_content_sha256(
        replay_input_domain,
        replay_input_payload,
    )
    calculation_payload = {
        "profile_content_sha256": request.profile_content_sha256,
        "tariff_compiler_content_sha256": bundle.compiler_content_sha256,
        "account_facts_sha256": eligibility.account_facts_sha256,
        "replay_input_sha256": replay_input_hash,
        "reconciliation_input_sha256": reconciliation.input_sha256 if reconciliation else None,
        "reconciliation_policy_sha256": reconciliation.policy_sha256 if reconciliation else None,
    }
    calculation_domain = (
        b"RateReplay.HistoricalReplayCalculation.v2"
        if isinstance(request, IntervalReplayRequest)
        else b"RateReplay.HistoricalReplayCalculation.v1"
    )
    calculation_hash = canonical_content_sha256(calculation_domain, calculation_payload)
    manifest: CalculationManifest | IntervalCalculationManifest
    if isinstance(request, IntervalReplayRequest):
        combined_period_energy: dict[str, int] = {}
        for schedule_totals in period_energy_by_schedule.values():
            for period, energy_wh in schedule_totals.items():
                combined_period_energy[period] = combined_period_energy.get(period, 0) + energy_wh
        manifest = IntervalCalculationManifest(
            manifest_version="historical-replay-manifest-v2",
            calculation_time_mode="HISTORICAL_REPLAY",
            profile_content_sha256=request.profile_content_sha256,
            tariff_compiler_content_sha256=bundle.compiler_content_sha256,
            tariff_ir_version=bundle.ir.ir_version,
            tariff_version_id=bundle.ir.tariff_version_id,
            account_facts_sha256=eligibility.account_facts_sha256,
            replay_input_sha256=replay_input_hash,
            reconciliation_input_sha256=(reconciliation.input_sha256 if reconciliation else None),
            reconciliation_policy_sha256=(reconciliation.policy_sha256 if reconciliation else None),
            baseline_allowance_wh=baseline_wh,
            billing_days=billing_days,
            bill_cycle_month=bill_cycle_month,
            period_energy_wh=dict(sorted(combined_period_energy.items())),
            calculation_sha256=calculation_hash,
        )
    else:
        manifest = CalculationManifest(
            manifest_version="e1-calculation-manifest-v1",
            calculation_time_mode="HISTORICAL_REPLAY",
            profile_content_sha256=request.profile_content_sha256,
            tariff_compiler_content_sha256=bundle.compiler_content_sha256,
            tariff_ir_version=bundle.ir.ir_version,
            tariff_version_id=bundle.ir.tariff_version_id,
            account_facts_sha256=eligibility.account_facts_sha256,
            replay_input_sha256=replay_input_hash,
            reconciliation_input_sha256=(reconciliation.input_sha256 if reconciliation else None),
            reconciliation_policy_sha256=(reconciliation.policy_sha256 if reconciliation else None),
            baseline_allowance_wh=baseline_wh,
            billing_days=billing_days,
            bill_cycle_month=bill_cycle_month,
            calculation_sha256=calculation_hash,
        )
    provenance_sources = tuple(
        cast(dict[str, object], source.model_dump(mode="json"))
        for source in bundle.reports.source_coverage
    )
    result_payload = {
        "eligibility": eligibility.model_dump(mode="json"),
        "supported_calculated_cents": supported_cents,
        "line_items": [line.model_dump(mode="json") for line in lines],
        "tariff_unsupported_placeholders": [item.model_dump(mode="json") for item in placeholders],
        "user_unsupported_lines": [
            item.model_dump(mode="json") for item in request.user_unsupported_lines
        ],
        "reconciliation": reconciliation.model_dump(mode="json") if reconciliation else None,
        "provenance_sources": provenance_sources,
        "manifest": manifest.model_dump(mode="json"),
    }
    return ReplayResult(
        result_version=(
            "historical-replay-result-v2"
            if isinstance(request, IntervalReplayRequest)
            else "e1-replay-result-v1"
        ),
        eligibility=eligibility,
        supported_calculated_cents=supported_cents,
        line_items=tuple(lines),
        tariff_unsupported_placeholders=tuple(placeholders),
        user_unsupported_lines=request.user_unsupported_lines,
        reconciliation=reconciliation,
        provenance_sources=provenance_sources,
        manifest=manifest,
        result_sha256=canonical_content_sha256(
            b"RateReplay.HistoricalReplayResult.v1", result_payload
        ),
    )
