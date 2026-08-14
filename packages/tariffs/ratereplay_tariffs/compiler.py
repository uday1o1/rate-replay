"""Fail-closed tariff compiler for source-locked declarative definitions."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from ratereplay_tariffs.compiled import (
    CanonicalChargeIR,
    CompilationBundle,
    CompilationReports,
    CompiledRule,
    CoverageReport,
    GoldenCoverage,
    IRBaselineAllowance,
    IREnergyTier,
    IRExplicitUnsupportedCharge,
    IRFixedDailyCharge,
    IRFixedMonthlyCharge,
    IRTieredEnergyCharge,
    IRTimeOfUseEnergyCharge,
    IRTimeOfUsePeriodRate,
    IRTimeOfUseSchedule,
    IRTimeOfUseWindow,
    SourceCoverage,
)
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import (
    BaselineAllowance,
    ExplicitUnsupportedCharge,
    FixedDailyCharge,
    FixedMonthlyCharge,
    TariffRule,
    TariffVersion,
    TieredEnergyCharge,
    TimeOfUseEnergyCharge,
    TimeOfUseSchedule,
)

SIGNED_INT64_MAX = 2**63 - 1
MAXIMUM_COMPILED_ENERGY_WH = 100_000_000
MAXIMUM_COMPILED_BILLING_DAYS = 366
_ALLOWED_UNITS = {
    "Wh/day",
    "Wh",
    "microdollars/kWh",
    "microdollars/day",
    "microdollars/bill_cycle",
}


class TariffCompileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _read_object(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise TariffCompileError("INVALID_JSON", f"Cannot read tariff lock {path}") from error
    if not isinstance(loaded, dict):
        raise TariffCompileError("INVALID_JSON", f"Tariff lock {path} must contain an object")
    return cast(dict[str, Any], loaded)


def _prevalidate_units(raw: dict[str, Any]) -> None:
    rules = raw.get("charge_rules", [])
    if not isinstance(rules, list):
        return
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        for key in ("unit", "quantity_unit", "rate_unit"):
            unit = rule.get(key)
            if unit is not None and unit not in _ALLOWED_UNITS:
                raise TariffCompileError("UNKNOWN_UNIT", f"Unknown tariff unit {unit!r}")


def load_tariff_definition(path: Path) -> TariffVersion:
    raw = _read_object(path)
    _prevalidate_units(raw)
    try:
        return TariffVersion.model_validate_json(path.read_bytes())
    except ValidationError as error:
        message = str(error)
        if "energy tier" in message:
            code = "INVALID_TIER"
        elif "nonholiday schedules require" in message or "calendar identifier" in message:
            code = "CALENDAR_LOCK_MISSING"
        else:
            code = "SCHEMA_INVALID"
        raise TariffCompileError(code, f"Tariff definition is invalid: {message}") from error


def _source_records(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    source_lock = _read_object(root / "tariffs/sources.lock.json")
    sources = source_lock.get("sources")
    if not isinstance(sources, list):
        raise TariffCompileError("SOURCE_LOCK_INVALID", "Source lock has no sources list")
    by_id: dict[str, dict[str, Any]] = {}
    for item in sources:
        if not isinstance(item, dict) or not isinstance(item.get("source_id"), str):
            raise TariffCompileError("SOURCE_LOCK_INVALID", "Source lock record is malformed")
        source_id = cast(str, item["source_id"])
        if source_id in by_id:
            raise TariffCompileError("SOURCE_LOCK_INVALID", f"Duplicate source {source_id}")
        by_id[source_id] = cast(dict[str, Any], item)
    return source_lock, by_id


def _validate_source_link(
    source_id: str,
    source_sha256: str,
    rule_id: str,
    source_records: dict[str, dict[str, Any]],
) -> None:
    record = source_records.get(source_id)
    if record is None:
        raise TariffCompileError("SOURCE_MISSING", f"Source {source_id} is not locked")
    if record.get("sha256") != source_sha256:
        raise TariffCompileError("SOURCE_HASH_MISMATCH", f"Source hash mismatch for {source_id}")
    locked_rules = record.get("rule_ids")
    if not isinstance(locked_rules, list) or rule_id not in locked_rules:
        raise TariffCompileError(
            "SOURCE_RULE_MISMATCH", f"Rule {rule_id} is not linked by source {source_id}"
        )
    if not record.get("source_url") and not record.get("archive_location"):
        raise TariffCompileError("SOURCE_UNRETRIEVABLE", f"Source {source_id} has no retrieval ID")


def _validate_windows(tariff: TariffVersion) -> None:
    ordered = sorted(tariff.admitted_service_windows, key=lambda item: item.start)
    for index, window in enumerate(ordered):
        if window.end is None:
            raise TariffCompileError("UNBOUNDED_ADMISSION", "Admitted windows must be bounded")
        if index:
            previous_end = ordered[index - 1].end
            if previous_end is not None and previous_end > window.start:
                raise TariffCompileError("ADMISSION_WINDOW_OVERLAP", "Admitted windows overlap")
    component_keys = [component.component_key for component in tariff.component_versions]
    if len(component_keys) != len(set(component_keys)):
        raise TariffCompileError("COMPONENT_OVERLAP", "Component vector contains a duplicate key")
    for component in tariff.component_versions:
        for window in tariff.admitted_service_windows:
            if not component.effective_range.covers(window):
                raise TariffCompileError(
                    "COMPONENT_COVERAGE_GAP",
                    f"Component {component.component_key} does not cover {window.start}",
                )
    for rule in tariff.charge_rules:
        for window in tariff.admitted_service_windows:
            if not rule.effective_range.covers(window):
                raise TariffCompileError(
                    "RULE_COVERAGE_GAP", f"Rule {rule.rule_id} does not cover {window.start}"
                )


def _validate_component_composition(
    tariff: TariffVersion,
    source_lock: dict[str, Any],
    source_records: dict[str, dict[str, Any]],
) -> None:
    vectors = source_lock.get("tariff_component_vectors")
    if not isinstance(vectors, list):
        raise TariffCompileError("COMPONENT_LOCK_INVALID", "Component-vector lock is missing")
    locked_vector = next(
        (
            item
            for item in vectors
            if isinstance(item, dict) and item.get("tariff_id") == tariff.tariff_version_id
        ),
        None,
    )
    if not isinstance(locked_vector, dict):
        raise TariffCompileError("COMPONENT_LOCK_MISSING", "No component vector is locked")
    expected_window = [
        tariff.admitted_service_windows[0].start.isoformat(),
        tariff.admitted_service_windows[0].end.isoformat()
        if tariff.admitted_service_windows[0].end
        else None,
    ]
    if locked_vector.get("service_window") != expected_window:
        raise TariffCompileError("COMPONENT_WINDOW_MISMATCH", "Locked component window differs")
    locked_components = locked_vector.get("components")
    if not isinstance(locked_components, list):
        raise TariffCompileError("COMPONENT_LOCK_INVALID", "Locked components are malformed")
    locked_by_key = {
        item.get("component_id"): item for item in locked_components if isinstance(item, dict)
    }
    extracted_owners: dict[str, list[str]] = defaultdict(list)
    for component in tariff.component_versions:
        locked = locked_by_key.get(component.component_key)
        if not isinstance(locked, dict):
            raise TariffCompileError(
                "COMPONENT_LOCK_MISMATCH", f"Component {component.component_key} is not locked"
            )
        expected_range = [
            component.effective_range.start.isoformat(),
            component.effective_range.end.isoformat() if component.effective_range.end else None,
        ]
        if (
            locked.get("effective_range") != expected_range
            or locked.get("precedence") != component.precedence
            or locked.get("source_id") != component.source.source_id
        ):
            raise TariffCompileError(
                "COMPONENT_LOCK_MISMATCH", f"Component {component.component_key} differs from lock"
            )
        for rule_id in component.extracted_rule_ids:
            _validate_source_link(
                component.source.source_id,
                component.source.source_sha256,
                rule_id,
                source_records,
            )
            extracted_owners[rule_id].append(component.component_key)
    required_rule_ids = {
        *(rule.rule_id for rule in tariff.charge_rules),
        *tariff.eligibility_predicate.source_rule_ids,
    }
    for rule_id in required_rule_ids:
        if len(extracted_owners.get(rule_id, [])) != 1:
            raise TariffCompileError(
                "RULE_COMPONENT_AMBIGUITY", f"Rule {rule_id} must belong to exactly one component"
            )
    for rule in tariff.charge_rules:
        owner_key = extracted_owners[rule.rule_id][0]
        owner = next(item for item in tariff.component_versions if item.component_key == owner_key)
        if rule.source.source_id != owner.source.source_id:
            raise TariffCompileError(
                "RULE_COMPONENT_SOURCE_MISMATCH",
                f"Rule {rule.rule_id} source differs from its component",
            )


def _validate_target_account(root: Path, tariff: TariffVersion) -> None:
    lock = _read_object(root / "tariffs/admission/target-account-v1.json")
    if (
        tariff.eligibility_predicate.predicate_version == "eligibility-predicate-v1"
        and lock.get("predicate_id") != tariff.eligibility_predicate.predicate_id
    ):
        raise TariffCompileError("ELIGIBILITY_LOCK_MISMATCH", "Eligibility predicate ID differs")
    expected = lock.get("required_facts")
    predicate = tariff.eligibility_predicate
    locked_values: dict[str, object] = {
        "service_provider": predicate.required_service_provider,
        "service_mode": predicate.required_service_mode,
        "meter_count": predicate.required_meter_count,
        "primary_meter_only": predicate.requires_primary_meter_only,
        "income_tier": predicate.supported_income_tiers[0],
        "care_enrolled": predicate.requires_care_enrolled,
        "fera_enrolled": predicate.requires_fera_enrolled,
        "medical_baseline": predicate.requires_medical_baseline,
        "cca_service": predicate.requires_cca_service,
        "direct_access_service": predicate.requires_direct_access_service,
        "active_bill_protection": predicate.requires_active_bill_protection,
        "solar_or_export": predicate.requires_solar_or_export,
        "service_window": [
            tariff.admitted_service_windows[0].start.isoformat(),
            tariff.admitted_service_windows[0].end.isoformat()
            if tariff.admitted_service_windows[0].end
            else None,
        ],
    }
    if expected != locked_values:
        raise TariffCompileError("ELIGIBILITY_LOCK_MISMATCH", "Target account facts differ")


def _validate_bounds(tariff: TariffVersion) -> None:
    maximum_rate = 0
    fixed_bound = 0
    for rule in tariff.charge_rules:
        if isinstance(rule, TieredEnergyCharge):
            maximum_rate = max(
                maximum_rate, *(tier.rate_microdollars_per_kwh for tier in rule.tiers)
            )
            for tier in rule.tiers:
                if tier.upper_bound_numerator > SIGNED_INT64_MAX // MAXIMUM_COMPILED_ENERGY_WH:
                    raise TariffCompileError(
                        "INT64_OVERFLOW", f"Tier {rule.rule_id} bound overflows"
                    )
        elif isinstance(rule, TimeOfUseEnergyCharge):
            largest_period_rate = max(
                period.rate_microdollars_per_kwh for period in rule.period_rates
            )
            credit = abs(rule.baseline_credit_microdollars_per_kwh or 0)
            maximum_rate = max(maximum_rate, largest_period_rate + credit)
        elif isinstance(rule, FixedDailyCharge):
            fixed_bound += abs(rule.rate_microdollars_per_day) * MAXIMUM_COMPILED_BILLING_DAYS
        elif isinstance(rule, FixedMonthlyCharge):
            fixed_bound += abs(rule.amount_microdollars)
    energy_bound = MAXIMUM_COMPILED_ENERGY_WH * maximum_rate
    if max(energy_bound, fixed_bound, energy_bound + fixed_bound) > SIGNED_INT64_MAX:
        raise TariffCompileError("INT64_OVERFLOW", "Tariff intermediate bound exceeds int64")


def _validate_time_operators(tariff: TariffVersion) -> None:
    schedules = {
        rule.rule_id: rule for rule in tariff.charge_rules if isinstance(rule, TimeOfUseSchedule)
    }
    baseline_ids = {
        rule.rule_id for rule in tariff.charge_rules if isinstance(rule, BaselineAllowance)
    }
    referenced_schedules: set[str] = set()
    for rule in tariff.charge_rules:
        if not isinstance(rule, TimeOfUseEnergyCharge):
            continue
        schedule = schedules.get(rule.schedule_rule_id)
        if schedule is None:
            raise TariffCompileError(
                "TOU_SCHEDULE_MISSING", f"Schedule {rule.schedule_rule_id} is not defined"
            )
        referenced_schedules.add(rule.schedule_rule_id)
        schedule_periods = {
            schedule.default_period,
            *(window.period for window in schedule.windows),
        }
        rate_periods = {period.period for period in rule.period_rates}
        if rate_periods != schedule_periods:
            raise TariffCompileError(
                "TOU_PERIOD_COVERAGE_MISMATCH",
                f"Rate periods do not exactly cover schedule {schedule.rule_id}",
            )
        if rule.baseline_rule_id is not None and rule.baseline_rule_id not in baseline_ids:
            raise TariffCompileError(
                "BASELINE_RULE_MISSING", f"Baseline rule {rule.baseline_rule_id} is not defined"
            )
    if set(schedules) != referenced_schedules:
        raise TariffCompileError("TOU_SCHEDULE_UNUSED", "Every time schedule must be used exactly")


def _load_golden_coverage(root: Path, tariff: TariffVersion) -> GoldenCoverage:
    if tariff.plan_code == "E-1":
        complete = _read_object(root / "tariffs/golden/e1-july-2026-complete-bill.json")
        boundaries = _read_object(root / "tariffs/golden/e1-july-2026-boundaries.json")
        cases: list[dict[str, Any]] = [complete]
        boundary_cases = boundaries.get("cases")
        suite_source_ids: list[str] = []
    else:
        golden_lock = _read_object(root / "tariffs/admission/m3-golden-lock.json")
        locked_tariff = next(
            (
                item
                for item in golden_lock.get("tariffs", [])
                if isinstance(item, dict) and item.get("plan_code") == tariff.plan_code
            ),
            None,
        )
        if not isinstance(locked_tariff, dict):
            raise TariffCompileError("GOLDEN_LOCK_MISSING", "Tariff golden lock is missing")
        suite = _read_object(root / cast(str, locked_tariff["golden_path"]))
        candidate_complete = suite.get("complete_bill")
        boundary_cases = suite.get("boundary_cases")
        suite_source_ids = cast(list[str], suite.get("source_ids", []))
        if not isinstance(candidate_complete, dict):
            raise TariffCompileError("GOLDEN_INVALID", "Complete-bill golden is malformed")
        complete = candidate_complete
        cases = [complete]
    if not isinstance(boundary_cases, list):
        raise TariffCompileError("GOLDEN_INVALID", "Boundary golden cases are malformed")
    cases.extend(cast(list[dict[str, Any]], boundary_cases))
    case_ids = tuple(sorted(cast(str, case["case_id"]) for case in cases))
    if case_ids != tariff.golden_case_ids:
        raise TariffCompileError("GOLDEN_LOCK_MISMATCH", "Golden case identifiers differ")
    by_rule: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        source_ids = case.get("source_ids", suite_source_ids)
        if source_ids is None:
            source_id = case.get("source_id")
            source_ids = [source_id] if isinstance(source_id, str) else []
        if not set(cast(list[str], source_ids)) <= set(tariff.source_ids):
            raise TariffCompileError(
                "GOLDEN_SOURCE_MISMATCH", "Golden references an unknown source"
            )
        rule_ids = case.get("rule_ids")
        if rule_ids is None:
            continue
        if not isinstance(rule_ids, list):
            raise TariffCompileError("GOLDEN_INVALID", "Golden rule identifiers are malformed")
        for rule_id in rule_ids:
            if isinstance(rule_id, str):
                by_rule[rule_id].append(cast(str, case["case_id"]))
    required = {
        *(rule.rule_id for rule in tariff.charge_rules),
        *tariff.eligibility_predicate.source_rule_ids,
    }
    missing = sorted(rule_id for rule_id in required if not by_rule.get(rule_id))
    if missing:
        raise TariffCompileError("GOLDEN_COVERAGE_GAP", f"Rules lack goldens: {missing}")
    return GoldenCoverage(
        golden_case_ids=case_ids,
        rule_case_ids={key: tuple(sorted(value)) for key, value in sorted(by_rule.items())},
    )


def _lower_rule(root: Path, rule: TariffRule) -> CompiledRule:
    if isinstance(rule, BaselineAllowance):
        return IRBaselineAllowance(
            operator="BASELINE_ALLOWANCE",
            rule_id=rule.rule_id,
            effective_range=rule.effective_range,
            applicability=rule.applicability,
            source=rule.source,
            rounding=rule.rounding,
            charge_component_key=rule.charge_component_key,
            daily_allowance_wh=rule.daily_allowance_wh,
        )
    if isinstance(rule, TieredEnergyCharge):
        return IRTieredEnergyCharge(
            operator="TIER_ALLOCATE_AND_MULTIPLY_RATIONAL",
            rule_id=rule.rule_id,
            effective_range=rule.effective_range,
            applicability=rule.applicability,
            source=rule.source,
            rounding=rule.rounding,
            charge_component_key=rule.charge_component_key,
            line_item_key=rule.line_item_key,
            tiers=tuple(
                IREnergyTier(
                    upper_bound_operator=tier.upper_bound_kind,
                    upper_bound_numerator=tier.upper_bound_numerator,
                    upper_bound_denominator=tier.upper_bound_denominator,
                    rate_microdollars_per_kwh=tier.rate_microdollars_per_kwh,
                )
                for tier in rule.tiers
            ),
        )
    if isinstance(rule, TimeOfUseSchedule):
        holiday_dates: tuple[str, ...] = ()
        if rule.calendar_id is not None:
            calendar = _read_object(root / "tariffs/calendars/ca-observed-holidays-2026.json")
            if calendar.get("calendar_id") != rule.calendar_id:
                raise TariffCompileError("CALENDAR_LOCK_MISSING", "Calendar identifier differs")
            if calendar.get("content_sha256") != rule.calendar_content_sha256:
                raise TariffCompileError("CALENDAR_HASH_MISMATCH", "Calendar content hash differs")
            holidays = calendar.get("holidays_used_in_july_window")
            if not isinstance(holidays, list):
                raise TariffCompileError("CALENDAR_LOCK_INVALID", "Calendar holidays are malformed")
            holiday_dates = tuple(
                sorted(
                    cast(str, item["date"])
                    for item in holidays
                    if isinstance(item, dict) and isinstance(item.get("date"), str)
                )
            )
        return IRTimeOfUseSchedule(
            operator="CLASSIFY_LOCAL_TIME_PERIOD",
            rule_id=rule.rule_id,
            effective_range=rule.effective_range,
            applicability=rule.applicability,
            source=rule.source,
            rounding=rule.rounding,
            charge_component_key=rule.charge_component_key,
            timezone=rule.timezone,
            windows=tuple(
                IRTimeOfUseWindow(
                    period=window.period,
                    start_minute_inclusive=window.start_minute_inclusive,
                    end_minute_exclusive=window.end_minute_exclusive,
                    day_selector=window.day_selector,
                )
                for window in rule.windows
            ),
            default_period=rule.default_period,
            calendar_id=rule.calendar_id,
            calendar_content_sha256=rule.calendar_content_sha256,
            holiday_dates=holiday_dates,
        )
    if isinstance(rule, TimeOfUseEnergyCharge):
        return IRTimeOfUseEnergyCharge(
            operator="TIME_OF_USE_MULTIPLY_WITH_OPTIONAL_BASELINE_CREDIT",
            rule_id=rule.rule_id,
            effective_range=rule.effective_range,
            applicability=rule.applicability,
            source=rule.source,
            rounding=rule.rounding,
            charge_component_key=rule.charge_component_key,
            line_item_key=rule.line_item_key,
            schedule_rule_id=rule.schedule_rule_id,
            period_rates=tuple(
                IRTimeOfUsePeriodRate(
                    period=period.period,
                    rate_microdollars_per_kwh=period.rate_microdollars_per_kwh,
                )
                for period in rule.period_rates
            ),
            baseline_credit_microdollars_per_kwh=(rule.baseline_credit_microdollars_per_kwh),
            baseline_rule_id=rule.baseline_rule_id,
        )
    if isinstance(rule, FixedDailyCharge):
        return IRFixedDailyCharge(
            operator="MULTIPLY_DAYS_BY_INTEGER_RATE",
            rule_id=rule.rule_id,
            effective_range=rule.effective_range,
            applicability=rule.applicability,
            source=rule.source,
            rounding=rule.rounding,
            charge_component_key=rule.charge_component_key,
            line_item_key=rule.line_item_key,
            rate_microdollars_per_day=rule.rate_microdollars_per_day,
        )
    if isinstance(rule, FixedMonthlyCharge):
        return IRFixedMonthlyCharge(
            operator="APPLICABILITY_GATED_INTEGER_AMOUNT",
            rule_id=rule.rule_id,
            effective_range=rule.effective_range,
            applicability=rule.applicability,
            source=rule.source,
            rounding=rule.rounding,
            charge_component_key=rule.charge_component_key,
            line_item_key=rule.line_item_key,
            amount_microdollars=rule.amount_microdollars,
        )
    if isinstance(rule, ExplicitUnsupportedCharge):
        return IRExplicitUnsupportedCharge(
            operator="EMIT_UNSUPPORTED_PLACEHOLDER",
            rule_id=rule.rule_id,
            effective_range=rule.effective_range,
            applicability=rule.applicability,
            source=rule.source,
            rounding=rule.rounding,
            charge_component_key=rule.charge_component_key,
            line_item_key=rule.line_item_key,
            reason_code=rule.reason_code,
        )
    raise AssertionError(f"Unhandled rule type {type(rule)}")


def compile_tariff(root: Path, definition_path: Path | None = None) -> CompilationBundle:
    """Compile one immutable tariff only after every lock and proof passes."""

    path = definition_path or root / "tariffs/definitions/pge-e1-2026-07.json"
    tariff = load_tariff_definition(path)
    source_lock, source_records = _source_records(root)
    _validate_windows(tariff)
    _validate_component_composition(tariff, source_lock, source_records)
    _validate_target_account(root, tariff)
    _validate_bounds(tariff)
    _validate_time_operators(tariff)
    for rule in tariff.charge_rules:
        _validate_source_link(
            rule.source.source_id,
            rule.source.source_sha256,
            rule.rule_id,
            source_records,
        )
    golden_coverage = _load_golden_coverage(root, tariff)
    normalized_ast = cast(dict[str, object], tariff.model_dump(mode="json"))
    normalized_ast_hash = canonical_content_sha256(b"RateReplay.TariffAST.v1", normalized_ast)
    ir = CanonicalChargeIR(
        ir_version="compiled-charge-ir-v1",
        tariff_version_id=tariff.tariff_version_id,
        maximum_energy_wh=MAXIMUM_COMPILED_ENERGY_WH,
        maximum_billing_days=MAXIMUM_COMPILED_BILLING_DAYS,
        operators=tuple(_lower_rule(root, rule) for rule in tariff.charge_rules),
    )
    source_coverages = tuple(
        SourceCoverage(
            source_id=source_id,
            source_sha256=cast(str, source_records[source_id]["sha256"]),
            source_url=cast(str, source_records[source_id]["source_url"]),
            linked_rule_ids=tuple(
                sorted(
                    {
                        *(
                            rule.rule_id
                            for rule in tariff.charge_rules
                            if rule.source.source_id == source_id
                        ),
                        *(
                            tariff.eligibility_predicate.source_rule_ids
                            if source_id == "pge-advice-7846-e"
                            else ()
                        ),
                    }
                )
            ),
        )
        for source_id in tariff.source_ids
    )
    reports = CompilationReports(
        normalized_ast_sha256=normalized_ast_hash,
        eligibility_predicate_id=tariff.eligibility_predicate.predicate_id,
        component_vector=CoverageReport(
            service_windows=tariff.admitted_service_windows,
            complete_component_keys=tuple(
                component.component_key for component in tariff.component_versions
            ),
            active_component_count_by_key=tuple(1 for _ in tariff.component_versions),
        ),
        source_coverage=source_coverages,
        golden_coverage=golden_coverage,
        solver_lowering_supported_operators=(),
        solver_lowering_unsupported_reasons=(cast(str, tariff.optimization_unsupported_reason),),
    )
    content_payload = {
        "normalized_ast": normalized_ast,
        "ir": ir.model_dump(mode="json"),
        "reports": reports.model_dump(mode="json"),
    }
    return CompilationBundle(
        bundle_version="tariff-compilation-bundle-v1",
        normalized_ast=normalized_ast,
        ir=ir,
        reports=reports,
        compiler_content_sha256=canonical_content_sha256(
            b"RateReplay.TariffCompilationBundle.v1", content_payload
        ),
    )
