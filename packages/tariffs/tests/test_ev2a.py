from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from ratereplay_tariffs.billing import (
    IntervalCalculationManifest,
    IntervalReplayRequest,
    ReplayError,
    ReplayInterval,
    evaluate_eligibility,
    replay_compiled_tariff,
)
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

ROOT = Path(__file__).resolve().parents[3]
DEFINITION = ROOT / "tariffs/definitions/pge-ev2a-2026-07.json"
ACCOUNT_FIXTURE = ROOT / "tariffs/examples/m3-comparison-account.json"


def _fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(ACCOUNT_FIXTURE.read_text(encoding="utf-8")))


def _account(*, technologies: list[str] | None = None) -> AccountFacts:
    payload = _fixture()["account_facts"]
    if technologies is not None:
        payload["qualifying_technologies"] = technologies
    return AccountFacts.model_validate_json(json.dumps(payload))


def _account_without_technology_fact() -> AccountFacts:
    payload = _fixture()["account_facts"]
    payload["qualifying_technologies"] = None
    return AccountFacts.model_validate_json(json.dumps(payload))


def _dated_facts(**updates: object) -> DatedEligibilityFacts:
    payload = _fixture()["dated_eligibility_facts"]
    payload.update(updates)
    return DatedEligibilityFacts.model_validate_json(json.dumps(payload))


def _interval(local_start: datetime, *, energy_wh: int = 1000) -> ReplayInterval:
    return ReplayInterval(
        start_utc_ns=int(local_start.astimezone(UTC).timestamp()) * 1_000_000_000,
        duration_seconds=3600,
        energy_wh=energy_wh,
    )


def _hourly_july() -> tuple[ReplayInterval, ...]:
    timezone = ZoneInfo("America/Los_Angeles")
    local_start = datetime(2026, 7, 1, tzinfo=timezone)
    local_end = datetime(2026, 8, 1, tzinfo=timezone)
    intervals: list[ReplayInterval] = []
    while local_start < local_end:
        intervals.append(_interval(local_start))
        local_start += timedelta(hours=1)
    return tuple(intervals)


def _request(
    intervals: tuple[ReplayInterval, ...] | None = None,
    *,
    dated_facts: DatedEligibilityFacts | None = None,
) -> IntervalReplayRequest:
    values = intervals or _hourly_july()
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256="1" * 64,
        account_facts=_account(),
        energy_wh=sum(interval.energy_wh for interval in values),
        intervals=values,
        dated_eligibility_facts=dated_facts or _dated_facts(),
    )


def test_ev2a_complete_bill_matches_prefrozen_golden() -> None:
    golden = json.loads((ROOT / "tariffs/golden/ev2a-july-2026.json").read_text(encoding="utf-8"))[
        "complete_bill"
    ]
    bundle = compile_tariff(ROOT, DEFINITION)
    assert bundle == compile_tariff(ROOT, DEFINITION)
    result = replay_compiled_tariff(bundle, _request())

    assert result.eligibility.status == golden["expected"]["eligibility_status"]
    assert [line.rounded_cents for line in result.line_items] == golden["expected"]["line_cents"]
    assert result.supported_calculated_cents == golden["expected"]["total_cents"]
    assert isinstance(result.manifest, IntervalCalculationManifest)
    assert result.manifest.period_energy_wh == golden["expected"]["period_energy_wh"]


@pytest.mark.parametrize(
    ("local_start", "expected_period"),
    [
        (datetime(2026, 7, 6, 15, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "PARTIAL_PEAK"),
        (datetime(2026, 7, 6, 16, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "PEAK"),
        (datetime(2026, 7, 6, 21, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "PARTIAL_PEAK"),
        (datetime(2026, 7, 7, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")), "OFF_PEAK"),
    ],
)
def test_ev2a_time_boundaries(local_start: datetime, expected_period: str) -> None:
    result = replay_compiled_tariff(
        compile_tariff(ROOT, DEFINITION), _request((_interval(local_start),))
    )
    assert isinstance(result.manifest, IntervalCalculationManifest)
    assert result.manifest.period_energy_wh == {expected_period: 1000}


def test_ev2a_missing_annual_usage_is_unknown_and_replay_is_refused() -> None:
    bundle = compile_tariff(ROOT, DEFINITION)
    dated_facts = _dated_facts(annual_usage_wh=None)
    eligibility = evaluate_eligibility(bundle, _account(), dated_facts)
    assert eligibility.status == "UNKNOWN"
    assert eligibility.reason_codes == ("ANNUAL_BASELINE_FACT_MISSING",)
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(bundle, _request(dated_facts=dated_facts))
    assert raised.value.code == "TARIFF_UNKNOWN"


def test_ev2a_exactly_eight_hundred_percent_is_eligible() -> None:
    dated_facts = _dated_facts(annual_usage_wh=16_000_000)
    result = replay_compiled_tariff(
        compile_tariff(ROOT, DEFINITION), _request(dated_facts=dated_facts)
    )
    assert result.eligibility.status == "ELIGIBLE"


def test_ev2a_over_eight_hundred_percent_is_ineligible() -> None:
    bundle = compile_tariff(ROOT, DEFINITION)
    dated_facts = _dated_facts(annual_usage_wh=16_000_001)
    eligibility = evaluate_eligibility(bundle, _account(), dated_facts)
    assert eligibility.status == "INELIGIBLE"
    assert eligibility.reason_codes == ("ANNUAL_BASELINE_LIMIT_EXCEEDED",)
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(bundle, _request(dated_facts=dated_facts))
    assert raised.value.code == "TARIFF_INELIGIBLE"


@pytest.mark.parametrize(
    ("registration", "expected_status", "expected_reason"),
    [
        (None, "UNKNOWN", "EV_REGISTRATION_FACT_MISSING"),
        (False, "INELIGIBLE", "EV_REGISTRATION_REQUIREMENT_MISMATCH"),
    ],
)
def test_ev2a_registration_is_tri_state(
    registration: bool | None, expected_status: str, expected_reason: str
) -> None:
    eligibility = evaluate_eligibility(
        compile_tariff(ROOT, DEFINITION),
        _account(),
        _dated_facts(ev_registered_and_charged_at_premises=registration),
    )
    assert eligibility.status == expected_status
    assert expected_reason in eligibility.reason_codes


def test_ev2a_missing_technology_fact_is_unknown() -> None:
    eligibility = evaluate_eligibility(
        compile_tariff(ROOT, DEFINITION),
        _account_without_technology_fact(),
        _dated_facts(),
    )
    assert eligibility.status == "UNKNOWN"
    assert "QUALIFYING_TECHNOLOGY_FACT_MISSING" in eligibility.reason_codes


def test_ev2a_separate_meter_is_ineligible() -> None:
    eligibility = evaluate_eligibility(
        compile_tariff(ROOT, DEFINITION),
        _account(),
        _dated_facts(whole_house_metering=False),
    )
    assert eligibility.status == "INELIGIBLE"
    assert "WHOLE_HOUSE_METERING_REQUIREMENT_MISMATCH" in eligibility.reason_codes


def test_ev2a_attestation_outside_window_is_unknown() -> None:
    eligibility = evaluate_eligibility(
        compile_tariff(ROOT, DEFINITION),
        _account(),
        _dated_facts(facts_as_of="2026-07-02"),
    )
    assert eligibility.status == "UNKNOWN"
    assert "DATED_TECHNOLOGY_FACT_OUTSIDE_WINDOW" in eligibility.reason_codes


def test_ev2a_wrong_annual_period_is_unknown() -> None:
    eligibility = evaluate_eligibility(
        compile_tariff(ROOT, DEFINITION),
        _account(),
        _dated_facts(annual_usage_period={"start": "2025-07-02", "end": "2026-07-01"}),
    )
    assert eligibility.status == "UNKNOWN"
    assert "ANNUAL_USAGE_PERIOD_MISMATCH" in eligibility.reason_codes


def test_ev2a_off_peak_rounding_boundary() -> None:
    local_start = datetime(2026, 7, 7, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    result = replay_compiled_tariff(
        compile_tariff(ROOT, DEFINITION),
        _request((_interval(local_start, energy_wh=23),)),
    )
    energy_line = next(
        line for line in result.line_items if line.line_item_key.endswith("off_peak")
    )
    assert Fraction(
        energy_line.pre_round_microdollars_numerator,
        energy_line.pre_round_microdollars_denominator,
    ) == Fraction(5_188_340, 1000)
    assert energy_line.rounded_cents == 1


def _peak_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["period_rates"][0]["rate_microdollars_per_kwh"] += 10000


def _partial_peak_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["period_rates"][1]["rate_microdollars_per_kwh"] += 10000


def _off_peak_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["period_rates"][2]["rate_microdollars_per_kwh"] += 10000


def _base_service(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["rate_microdollars_per_day"] += 10000


def _climate_credit(payload: dict[str, Any]) -> None:
    payload["charge_rules"][3]["amount_microdollars"] += 10000


def _afternoon_partial_start(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][0]["start_minute_inclusive"] = 840


def _afternoon_partial_end(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][0]["end_minute_exclusive"] = 930


def _peak_start(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][1]["start_minute_inclusive"] = 990


def _peak_end(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][1]["end_minute_exclusive"] = 1230


def _evening_partial_start(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][2]["start_minute_inclusive"] = 1290


def _evening_partial_end(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["windows"][2]["end_minute_exclusive"] = 1410


def _effective_date(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["effective_range"]["start"] = "2026-07-02"


@pytest.mark.parametrize(
    "mutation",
    [
        _peak_rate,
        _partial_peak_rate,
        _off_peak_rate,
        _base_service,
        _climate_credit,
        _afternoon_partial_start,
        _afternoon_partial_end,
        _peak_start,
        _peak_end,
        _evening_partial_start,
        _evening_partial_end,
        _effective_date,
    ],
    ids=lambda mutation: mutation.__name__.removeprefix("_"),
)
def test_each_ev2a_rate_or_boundary_breaks_prefrozen_golden(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    mutation(payload)
    mutated = tmp_path / "ev2a.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    try:
        result = replay_compiled_tariff(compile_tariff(ROOT, mutated), _request())
    except (ReplayError, TariffCompileError):
        return
    assert [line.rounded_cents for line in result.line_items] != [
        8340,
        5302,
        10489,
        2460,
        -3618,
    ]


def test_ev2a_annual_limit_mutation_rejects_common_account(tmp_path: Path) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    payload["eligibility_predicate"]["maximum_annual_baseline_ratio_numerator"] = 2
    mutated = tmp_path / "ev2a.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    bundle = compile_tariff(ROOT, mutated)
    eligibility = evaluate_eligibility(bundle, _account(), _dated_facts())
    assert eligibility.status == "INELIGIBLE"
    assert eligibility.reason_codes == ("ANNUAL_BASELINE_LIMIT_EXCEEDED",)
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(bundle, _request())
    assert raised.value.code == "TARIFF_INELIGIBLE"
