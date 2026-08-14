#!/usr/bin/env python3
"""Validate pre-production Milestone 3 goldens without production tariff imports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATHS = {
    "E-TOU-C": ROOT / "tariffs/golden/etouc-july-2026.json",
    "E-TOU-D": ROOT / "tariffs/golden/etoud-july-2026.json",
    "E-ELEC": ROOT / "tariffs/golden/eelec-july-2026.json",
    "EV2-A": ROOT / "tariffs/golden/ev2a-july-2026.json",
}

PeriodClassifier = Callable[[datetime], str]


class GoldenValidationError(RuntimeError):
    """A frozen independent golden does not satisfy its own derivation."""


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise GoldenValidationError(f"GOLDEN_NOT_OBJECT:{path.name}")
    return cast(dict[str, Any], value)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise GoldenValidationError(code)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _half_up(numerator: int, denominator: int) -> int:
    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return sign * quotient


def _round_microdollars_to_cents(numerator: int, denominator: int = 1) -> int:
    return _half_up(numerator, denominator * 10_000)


def _etouc(local_start: datetime) -> str:
    return "PEAK" if 16 <= local_start.hour < 21 else "OFF_PEAK"


def _etoud(holiday_dates: frozenset[date]) -> PeriodClassifier:
    def classify(local_start: datetime) -> str:
        is_peak = (
            local_start.weekday() < 5
            and local_start.date() not in holiday_dates
            and 17 <= local_start.hour < 20
        )
        return "PEAK" if is_peak else "OFF_PEAK"

    return classify


def _three_period(local_start: datetime) -> str:
    if 16 <= local_start.hour < 21:
        return "PEAK"
    if local_start.hour == 15 or 21 <= local_start.hour < 24:
        return "PARTIAL_PEAK"
    return "OFF_PEAK"


def _period_totals(classifier: PeriodClassifier) -> dict[str, int]:
    totals: dict[str, int] = {}
    day = date(2026, 7, 1)
    end = date(2026, 8, 1)
    while day < end:
        for hour in range(24):
            local_start = datetime.fromisoformat(f"{day.isoformat()}T{hour:02d}:00:00-07:00")
            period = classifier(local_start)
            totals[period] = totals.get(period, 0) + 1_000
        day += timedelta(days=1)
    return totals


def _validate_boundaries(
    plan_code: str, suite: dict[str, Any], classifier: PeriodClassifier
) -> None:
    for case in suite["boundary_cases"]:
        if "expected_period" not in case:
            continue
        observed_period = classifier(datetime.fromisoformat(case["local_start"]))
        _require(observed_period == case["expected_period"], f"{plan_code}:{case['case_id']}")
        _require(observed_period in {"PEAK", "PARTIAL_PEAK", "OFF_PEAK"}, "UNKNOWN_PERIOD")
    rounding_cases = [
        case for case in suite["boundary_cases"] if "pre_round_microdollars_numerator" in case
    ]
    for case in rounding_cases:
        observed_cents = _round_microdollars_to_cents(
            case["pre_round_microdollars_numerator"],
            case["pre_round_microdollars_denominator"],
        )
        _require(observed_cents == case["expected_cents"], f"{plan_code}:{case['case_id']}")


def _validate_complete_bill(
    plan_code: str, suite: dict[str, Any], classifier: PeriodClassifier
) -> None:
    complete = suite["complete_bill"]
    expected = complete["expected"]
    period_totals = _period_totals(classifier)
    _require(period_totals == expected["period_energy_wh"], f"{plan_code}:PERIOD_TOTALS")
    _require(sum(period_totals.values()) == 744_000, f"{plan_code}:PROFILE_TOTAL")

    line_cents = [
        _round_microdollars_to_cents(
            expected["period_energy_wh"][period] * rate,
            1_000,
        )
        for period, rate in expected["rate_microdollars_per_kwh"].items()
    ]
    if plan_code == "E-TOU-C":
        line_cents.append(
            _round_microdollars_to_cents(
                expected["baseline_credit_wh"] * expected["baseline_credit_microdollars_per_kwh"],
                1_000,
            )
        )
    line_cents.extend(
        [
            _round_microdollars_to_cents(
                complete["inputs"]["billing_days"] * expected["base_service_microdollars_per_day"]
            ),
            _round_microdollars_to_cents(expected["climate_credit_microdollars"]),
        ]
    )
    _require(line_cents == expected["line_cents"], f"{plan_code}:LINE_CENTS")
    _require(sum(line_cents) == expected["total_cents"], f"{plan_code}:TOTAL_CENTS")
    _require(
        expected["minimum_bill_adjustment"] == "NOT_APPLICABLE",
        f"{plan_code}:MINIMUM_BILL_SCOPE",
    )


def _validate_eligibility_facts() -> None:
    fixture = _load(ROOT / "tariffs/examples/m3-comparison-account.json")
    account = fixture["account_facts"]
    dated = fixture["dated_eligibility_facts"]
    _require(account["active_bill_protection"] is False, "ETOUC_BILL_PROTECTION_FACT")
    _require("EV" in account["qualifying_technologies"], "EELEC_TECHNOLOGY_FACT")
    _require(dated["facts_as_of"] == "2026-07-01", "DATED_FACT_BOUNDARY")
    _require(dated["ev_registered_and_charged_at_premises"] is True, "EV_FACT")
    _require(dated["whole_house_metering"] is True, "WHOLE_HOUSE_FACT")
    _require(
        dated["annual_usage_wh"] * 1 == 3 * dated["annual_baseline_allowance_wh"],
        "ANNUAL_BASELINE_RATIO",
    )
    _require(
        dated["annual_usage_wh"] <= 8 * dated["annual_baseline_allowance_wh"],
        "EV2A_ANNUAL_LIMIT",
    )


def validate() -> None:
    golden_lock = _load(ROOT / "tariffs/admission/m3-golden-lock.json")
    source_lock = _load(ROOT / "tariffs/sources.lock.json")
    _require(golden_lock["state"] == "GOLDENS_FROZEN_AND_ADMITTED", "LOCK_STATE")
    source_records = {item["source_id"]: item for item in source_lock["sources"]}
    for locked_source in golden_lock["sources"]:
        source = source_records[locked_source["source_id"]]
        _require(source["sha256"] == locked_source["sha256"], "SOURCE_HASH_LOCK")
        _require(source["source_url"] == locked_source["source_url"], "SOURCE_URL_LOCK")
        _require(locked_source["retrieved_and_hash_verified"] is True, "SOURCE_RETRIEVAL")
    account_lock = golden_lock["common_account_fixture"]
    _require(_sha256(ROOT / account_lock["path"]) == account_lock["sha256"], "ACCOUNT_HASH")
    for tariff_lock in golden_lock["tariffs"]:
        _require(tariff_lock["admission_status"] == "ADMITTED", "ADMISSION_STATUS")
        path = ROOT / tariff_lock["golden_path"]
        _require(_sha256(path) == tariff_lock["golden_sha256"], "GOLDEN_HASH")
        suite = _load(path)
        complete = suite["complete_bill"]
        _require(
            set(complete["rule_ids"]) == set(tariff_lock["required_rule_ids"]),
            "GOLDEN_RULE_COVERAGE",
        )
        _require(
            set(complete["source_sheets"]) == set(tariff_lock["source_sheets"]),
            "GOLDEN_SOURCE_SHEETS",
        )
    holiday_lock = _load(ROOT / "tariffs/calendars/ca-observed-holidays-2026.json")
    holiday_dates = frozenset(
        date.fromisoformat(item["date"]) for item in holiday_lock["holidays_used_in_july_window"]
    )
    _require(holiday_dates == {date(2026, 7, 3)}, "JULY_HOLIDAY_SET")
    classifiers: dict[str, PeriodClassifier] = {
        "E-TOU-C": _etouc,
        "E-TOU-D": _etoud(holiday_dates),
        "E-ELEC": _three_period,
        "EV2-A": _three_period,
    }
    for plan_code, path in GOLDEN_PATHS.items():
        suite = _load(path)
        _validate_complete_bill(plan_code, suite, classifiers[plan_code])
        _validate_boundaries(plan_code, suite, classifiers[plan_code])
    _validate_eligibility_facts()


def main() -> None:
    validate()
    print("All four independent Milestone 3 tariff golden suites are internally consistent.")


if __name__ == "__main__":
    main()
