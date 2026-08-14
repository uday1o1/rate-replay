"""Fail-closed tariff compiler for source-locked declarative definitions."""

from __future__ import annotations

import hashlib
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
    SourceCoverage,
)
from ratereplay_tariffs.schema import (
    BaselineAllowance,
    ExplicitUnsupportedCharge,
    FixedDailyCharge,
    FixedMonthlyCharge,
    TariffRule,
    TariffVersion,
    TieredEnergyCharge,
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


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _content_hash(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + b"\x00" + canonical_json_bytes(value)).hexdigest()


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
        code = "INVALID_TIER" if "energy tier" in message else "SCHEMA_INVALID"
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
    if lock.get("predicate_id") != tariff.eligibility_predicate.predicate_id:
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
        elif isinstance(rule, FixedDailyCharge):
            fixed_bound += abs(rule.rate_microdollars_per_day) * MAXIMUM_COMPILED_BILLING_DAYS
        elif isinstance(rule, FixedMonthlyCharge):
            fixed_bound += abs(rule.amount_microdollars)
    energy_bound = MAXIMUM_COMPILED_ENERGY_WH * maximum_rate
    if max(energy_bound, fixed_bound, energy_bound + fixed_bound) > SIGNED_INT64_MAX:
        raise TariffCompileError("INT64_OVERFLOW", "Tariff intermediate bound exceeds int64")


def _load_golden_coverage(root: Path, tariff: TariffVersion) -> GoldenCoverage:
    complete = _read_object(root / "tariffs/golden/e1-july-2026-complete-bill.json")
    boundaries = _read_object(root / "tariffs/golden/e1-july-2026-boundaries.json")
    cases: list[dict[str, Any]] = [complete]
    boundary_cases = boundaries.get("cases")
    if not isinstance(boundary_cases, list):
        raise TariffCompileError("GOLDEN_INVALID", "Boundary golden cases are malformed")
    cases.extend(cast(list[dict[str, Any]], boundary_cases))
    case_ids = tuple(sorted(cast(str, case["case_id"]) for case in cases))
    if case_ids != tariff.golden_case_ids:
        raise TariffCompileError("GOLDEN_LOCK_MISMATCH", "Golden case identifiers differ")
    by_rule: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        source_ids = case.get("source_ids")
        if source_ids is None:
            source_id = case.get("source_id")
            source_ids = [source_id] if isinstance(source_id, str) else []
        if not set(cast(list[str], source_ids)) <= set(tariff.source_ids):
            raise TariffCompileError(
                "GOLDEN_SOURCE_MISMATCH", "Golden references an unknown source"
            )
        rule_ids = case.get("rule_ids")
        if not isinstance(rule_ids, list):
            raise TariffCompileError("GOLDEN_INVALID", "Golden has no rule identifiers")
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


def _lower_rule(rule: TariffRule) -> CompiledRule:
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
    for rule in tariff.charge_rules:
        _validate_source_link(
            rule.source.source_id,
            rule.source.source_sha256,
            rule.rule_id,
            source_records,
        )
    golden_coverage = _load_golden_coverage(root, tariff)
    normalized_ast = cast(dict[str, object], tariff.model_dump(mode="json"))
    normalized_ast_hash = _content_hash(b"RateReplay.TariffAST.v1", normalized_ast)
    ir = CanonicalChargeIR(
        ir_version="compiled-charge-ir-v1",
        tariff_version_id=tariff.tariff_version_id,
        maximum_energy_wh=MAXIMUM_COMPILED_ENERGY_WH,
        maximum_billing_days=MAXIMUM_COMPILED_BILLING_DAYS,
        operators=tuple(_lower_rule(rule) for rule in tariff.charge_rules),
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
        compiler_content_sha256=_content_hash(
            b"RateReplay.TariffCompilationBundle.v1", content_payload
        ),
    )
