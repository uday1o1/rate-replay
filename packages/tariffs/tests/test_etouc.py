from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from ratereplay_tariffs.billing import (
    IntervalCalculationManifest,
    IntervalReplayRequest,
    ReplayError,
    ReplayInterval,
    ReplayRequest,
    evaluate_eligibility,
    replay_compiled_tariff,
)
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff
from ratereplay_tariffs.schema import AccountFacts

ROOT = Path(__file__).resolve().parents[3]
DEFINITION = ROOT / "tariffs/definitions/pge-etouc-2026-07.json"


def _account(**changes: object) -> AccountFacts:
    fixture = json.loads(
        (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
    )["account_facts"]
    fixture.update(changes)
    return AccountFacts.model_validate_json(json.dumps(fixture))


def _hourly_july() -> tuple[ReplayInterval, ...]:
    timezone = ZoneInfo("America/Los_Angeles")
    local_start = datetime(2026, 7, 1, tzinfo=timezone)
    local_end = datetime(2026, 8, 1, tzinfo=timezone)
    intervals: list[ReplayInterval] = []
    while local_start < local_end:
        start_utc = local_start.astimezone(UTC)
        intervals.append(
            ReplayInterval(
                start_utc_ns=int(start_utc.timestamp()) * 1_000_000_000,
                duration_seconds=3600,
                energy_wh=1000,
            )
        )
        local_start += timedelta(hours=1)
    return tuple(intervals)


def _request(
    *,
    account: AccountFacts | None = None,
    intervals: tuple[ReplayInterval, ...] | None = None,
) -> IntervalReplayRequest:
    values = intervals or _hourly_july()
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256="c" * 64,
        account_facts=account or _account(),
        energy_wh=sum(interval.energy_wh for interval in values),
        intervals=values,
    )


def _one_interval(local_start: datetime, duration_seconds: int = 900) -> ReplayInterval:
    return ReplayInterval(
        start_utc_ns=int(local_start.astimezone(UTC).timestamp()) * 1_000_000_000,
        duration_seconds=duration_seconds,
        energy_wh=1000,
    )


def test_etouc_compiles_deterministically_without_changing_e1() -> None:
    first = compile_tariff(ROOT, DEFINITION)
    second = compile_tariff(ROOT, DEFINITION)

    assert first == second
    assert first.ir.tariff_version_id == "pge-etouc-2026-07"
    assert len(first.ir.operators) == 5
    assert compile_tariff(ROOT).compiler_content_sha256 == (
        "b2e7fce980170d2e42332ea608612f0a14303564043c16d0a4b2e167456e57eb"
    )


def test_etouc_complete_bill_matches_prefrozen_golden() -> None:
    golden = json.loads((ROOT / "tariffs/golden/etouc-july-2026.json").read_text(encoding="utf-8"))[
        "complete_bill"
    ]
    result = replay_compiled_tariff(compile_tariff(ROOT, DEFINITION), _request())

    assert [line.rounded_cents for line in result.line_items] == golden["expected"]["line_cents"]
    assert result.supported_calculated_cents == golden["expected"]["total_cents"]
    assert result.line_items[2].line_item_key == "bundled_energy.baseline_credit"
    assert result.line_items[2].charge_component_key == "baseline_adjustment"
    assert isinstance(result.manifest, IntervalCalculationManifest)
    assert result.manifest.period_energy_wh == {"OFF_PEAK": 589000, "PEAK": 155000}
    assert sum(line.rounded_cents for line in result.line_items) == (
        result.supported_calculated_cents
    )
    allocation = result.diagnostic_cost_allocation
    assert allocation is not None
    assert allocation.status == "AVAILABLE"
    assert len({line.service_day for line in allocation.daily_energy_charges}) == 31
    assert sum(line.allocated_cents for line in allocation.daily_energy_charges) == 29_982
    assert allocation.monthly_energy_charges[0].calendar_month == "2026-07"
    assert allocation.monthly_energy_charges[0].allocated_cents == 29_982
    assert allocation.reconciliation.daily_energy_charge_cents == 29_982
    assert allocation.reconciliation.supported_period_adjustment_cents == -1_158
    assert allocation.reconciliation.supported_calculated_cents == (
        result.supported_calculated_cents
    )
    by_line = {
        line.line_item_key: sum(
            item.allocated_cents
            for item in allocation.daily_energy_charges
            if item.line_item_key == line.line_item_key
        )
        for line in result.line_items
        if line.quantity_unit == "Wh"
    }
    assert by_line == {
        "bundled_energy.baseline_credit": -1_640,
        "bundled_energy.off_peak": 23_525,
        "bundled_energy.peak": 8_097,
    }


@pytest.mark.parametrize(
    ("local_start", "expected_period"),
    [
        (datetime(2026, 7, 6, 15, 45, tzinfo=ZoneInfo("America/Los_Angeles")), "OFF_PEAK"),
        (datetime(2026, 7, 6, 16, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "PEAK"),
        (datetime(2026, 7, 6, 21, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "OFF_PEAK"),
        (datetime(2026, 7, 11, 18, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "PEAK"),
    ],
)
def test_etouc_time_boundaries(local_start: datetime, expected_period: str) -> None:
    interval = _one_interval(local_start)
    result = replay_compiled_tariff(
        compile_tariff(ROOT, DEFINITION), _request(intervals=(interval,))
    )

    assert isinstance(result.manifest, IntervalCalculationManifest)
    assert result.manifest.period_energy_wh == {expected_period: 1000}


def test_etouc_rejects_an_interval_crossing_peak_start() -> None:
    interval = _one_interval(
        datetime(2026, 7, 6, 15, 45, tzinfo=ZoneInfo("America/Los_Angeles")),
        duration_seconds=1800,
    )
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(compile_tariff(ROOT, DEFINITION), _request(intervals=(interval,)))
    assert raised.value.code == "INTERVAL_CROSSES_TARIFF_BOUNDARY"


def test_etouc_active_bill_protection_is_ineligible() -> None:
    eligibility = evaluate_eligibility(
        compile_tariff(ROOT, DEFINITION), _account(active_bill_protection=True)
    )
    assert eligibility.status == "INELIGIBLE"
    assert eligibility.reason_codes == ("BILL_PROTECTION_STATUS_MISMATCH",)


def test_etouc_requires_intervals() -> None:
    request = ReplayRequest(
        request_version="e1-replay-request-v1",
        profile_content_sha256="d" * 64,
        account_facts=_account(),
        energy_wh=1000,
    )
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(compile_tariff(ROOT, DEFINITION), request)
    assert raised.value.code == "INTERVAL_DATA_REQUIRED"


def test_etouc_missing_schedule_reference_fails(tmp_path: Path) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    payload["charge_rules"][2]["schedule_rule_id"] = "missing-schedule"
    mutated = tmp_path / "etouc.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TariffCompileError) as raised:
        compile_tariff(ROOT, mutated)
    assert raised.value.code == "TOU_SCHEDULE_MISSING"


def test_etouc_period_coverage_mutation_fails(tmp_path: Path) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    payload["charge_rules"][2]["period_rates"][1]["period"] = "PARTIAL_PEAK"
    mutated = tmp_path / "etouc.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TariffCompileError) as raised:
        compile_tariff(ROOT, mutated)
    assert raised.value.code == "TOU_PERIOD_COVERAGE_MISMATCH"


def _mutate_peak_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["period_rates"][0]["rate_microdollars_per_kwh"] += 10000


def _mutate_off_peak_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["period_rates"][1]["rate_microdollars_per_kwh"] += 10000


def _mutate_baseline_credit(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["baseline_credit_microdollars_per_kwh"] -= 10000


def _mutate_base_service(payload: dict[str, Any]) -> None:
    payload["charge_rules"][3]["rate_microdollars_per_day"] += 10000


def _mutate_climate_credit(payload: dict[str, Any]) -> None:
    payload["charge_rules"][4]["amount_microdollars"] += 10000


def _mutate_baseline_boundary(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["daily_allowance_wh"] += 1000


def _mutate_peak_start(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["windows"][0]["start_minute_inclusive"] = 900


def _mutate_peak_end(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["windows"][0]["end_minute_exclusive"] = 1200


def _mutate_effective_date(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["effective_range"]["start"] = "2026-07-02"


@pytest.mark.parametrize(
    "mutation",
    [
        _mutate_peak_rate,
        _mutate_off_peak_rate,
        _mutate_baseline_credit,
        _mutate_base_service,
        _mutate_climate_credit,
        _mutate_baseline_boundary,
        _mutate_peak_start,
        _mutate_peak_end,
        _mutate_effective_date,
    ],
    ids=lambda mutation: mutation.__name__.removeprefix("_mutate_"),
)
def test_each_etouc_rate_or_boundary_breaks_prefrozen_golden(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    mutation(payload)
    mutated = tmp_path / "etouc.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    try:
        result = replay_compiled_tariff(compile_tariff(ROOT, mutated), _request())
    except (ReplayError, TariffCompileError):
        return
    assert [line.rounded_cents for line in result.line_items] != [
        8097,
        23525,
        -1640,
        2460,
        -3618,
    ]
