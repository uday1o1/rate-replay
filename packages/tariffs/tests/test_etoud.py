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
    replay_compiled_tariff,
)
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff
from ratereplay_tariffs.schema import AccountFacts

ROOT = Path(__file__).resolve().parents[3]
DEFINITION = ROOT / "tariffs/definitions/pge-etoud-2026-07.json"


def _account() -> AccountFacts:
    fixture = json.loads(
        (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
    )["account_facts"]
    return AccountFacts.model_validate_json(json.dumps(fixture))


def _hourly_july() -> tuple[ReplayInterval, ...]:
    timezone = ZoneInfo("America/Los_Angeles")
    local_start = datetime(2026, 7, 1, tzinfo=timezone)
    local_end = datetime(2026, 8, 1, tzinfo=timezone)
    intervals: list[ReplayInterval] = []
    while local_start < local_end:
        intervals.append(_interval(local_start, duration_seconds=3600))
        local_start += timedelta(hours=1)
    return tuple(intervals)


def _interval(local_start: datetime, *, duration_seconds: int = 900) -> ReplayInterval:
    return ReplayInterval(
        start_utc_ns=int(local_start.astimezone(UTC).timestamp()) * 1_000_000_000,
        duration_seconds=duration_seconds,
        energy_wh=1000,
    )


def _request(intervals: tuple[ReplayInterval, ...] | None = None) -> IntervalReplayRequest:
    values = intervals or _hourly_july()
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256="e" * 64,
        account_facts=_account(),
        energy_wh=sum(interval.energy_wh for interval in values),
        intervals=values,
    )


def test_etoud_complete_bill_matches_prefrozen_golden() -> None:
    golden = json.loads((ROOT / "tariffs/golden/etoud-july-2026.json").read_text(encoding="utf-8"))[
        "complete_bill"
    ]
    bundle = compile_tariff(ROOT, DEFINITION)
    assert bundle == compile_tariff(ROOT, DEFINITION)
    result = replay_compiled_tariff(bundle, _request())

    assert [line.rounded_cents for line in result.line_items] == golden["expected"]["line_cents"]
    assert result.supported_calculated_cents == golden["expected"]["total_cents"]
    assert isinstance(result.manifest, IntervalCalculationManifest)
    assert result.manifest.period_energy_wh == {"OFF_PEAK": 678000, "PEAK": 66000}


@pytest.mark.parametrize(
    ("local_start", "expected_period"),
    [
        (datetime(2026, 7, 6, 17, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "PEAK"),
        (datetime(2026, 7, 6, 20, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "OFF_PEAK"),
        (datetime(2026, 7, 3, 18, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "OFF_PEAK"),
        (datetime(2026, 7, 11, 18, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "OFF_PEAK"),
    ],
)
def test_etoud_time_and_holiday_boundaries(local_start: datetime, expected_period: str) -> None:
    result = replay_compiled_tariff(
        compile_tariff(ROOT, DEFINITION), _request((_interval(local_start),))
    )
    assert isinstance(result.manifest, IntervalCalculationManifest)
    assert result.manifest.period_energy_wh == {expected_period: 1000}


def test_etoud_missing_calendar_fails_with_frozen_code(tmp_path: Path) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    payload["charge_rules"][0]["calendar_id"] = None
    payload["charge_rules"][0]["calendar_content_sha256"] = None
    mutated = tmp_path / "etoud.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TariffCompileError) as raised:
        compile_tariff(ROOT, mutated)
    assert raised.value.code == "CALENDAR_LOCK_MISSING"


def test_etoud_calendar_hash_mismatch_fails(tmp_path: Path) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    payload["charge_rules"][0]["calendar_content_sha256"] = "0" * 64
    mutated = tmp_path / "etoud.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TariffCompileError) as raised:
        compile_tariff(ROOT, mutated)
    assert raised.value.code == "CALENDAR_HASH_MISMATCH"


def _peak_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["period_rates"][0]["rate_microdollars_per_kwh"] += 10000


def _off_peak_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["period_rates"][1]["rate_microdollars_per_kwh"] += 10000


def _base_service(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["rate_microdollars_per_day"] += 10000


def _climate_credit(payload: dict[str, Any]) -> None:
    payload["charge_rules"][3]["amount_microdollars"] += 10000


def _peak_start(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][0]["start_minute_inclusive"] = 960


def _peak_end(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][0]["end_minute_exclusive"] = 1140


def _effective_date(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["effective_range"]["start"] = "2026-07-02"


@pytest.mark.parametrize(
    "mutation",
    [
        _peak_rate,
        _off_peak_rate,
        _base_service,
        _climate_credit,
        _peak_start,
        _peak_end,
        _effective_date,
    ],
    ids=lambda mutation: mutation.__name__.removeprefix("_"),
)
def test_each_etoud_rate_or_boundary_breaks_prefrozen_golden(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    mutation(payload)
    mutated = tmp_path / "etoud.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    try:
        result = replay_compiled_tariff(compile_tariff(ROOT, mutated), _request())
    except (ReplayError, TariffCompileError):
        return
    assert [line.rounded_cents for line in result.line_items] != [3149, 23196, 2460, -3618]
