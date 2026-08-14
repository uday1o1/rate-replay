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
DEFINITION = ROOT / "tariffs/definitions/pge-eelec-2026-07.json"
ACCOUNT_FIXTURE = ROOT / "tariffs/examples/m3-comparison-account.json"


def _fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(ACCOUNT_FIXTURE.read_text(encoding="utf-8")))


def _account(technologies: list[str] | None = None) -> AccountFacts:
    payload = _fixture()["account_facts"]
    if technologies is not None:
        payload["qualifying_technologies"] = technologies
    return AccountFacts.model_validate_json(json.dumps(payload))


def _account_without_technology_fact() -> AccountFacts:
    payload = _fixture()["account_facts"]
    payload["qualifying_technologies"] = None
    return AccountFacts.model_validate_json(json.dumps(payload))


def _dated_facts() -> DatedEligibilityFacts:
    return DatedEligibilityFacts.model_validate_json(
        json.dumps(_fixture()["dated_eligibility_facts"])
    )


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
    account: AccountFacts | None = None,
    dated_facts: DatedEligibilityFacts | None = None,
    omit_dated_facts: bool = False,
) -> IntervalReplayRequest:
    values = intervals or _hourly_july()
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256="f" * 64,
        account_facts=account or _account(),
        energy_wh=sum(interval.energy_wh for interval in values),
        intervals=values,
        dated_eligibility_facts=(None if omit_dated_facts else dated_facts or _dated_facts()),
    )


def test_eelec_complete_bill_matches_prefrozen_golden() -> None:
    golden = json.loads((ROOT / "tariffs/golden/eelec-july-2026.json").read_text(encoding="utf-8"))[
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
def test_eelec_time_boundaries(local_start: datetime, expected_period: str) -> None:
    result = replay_compiled_tariff(
        compile_tariff(ROOT, DEFINITION), _request((_interval(local_start),))
    )
    assert isinstance(result.manifest, IntervalCalculationManifest)
    assert result.manifest.period_energy_wh == {expected_period: 1000}


def test_eelec_missing_technology_fact_is_unknown() -> None:
    bundle = compile_tariff(ROOT, DEFINITION)
    account = _account_without_technology_fact()
    eligibility = evaluate_eligibility(bundle, account, _dated_facts())
    assert eligibility.status == "UNKNOWN"
    assert "QUALIFYING_TECHNOLOGY_FACT_MISSING" in eligibility.reason_codes
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(bundle, _request(account=account))
    assert raised.value.code == "TARIFF_UNKNOWN"


def test_eelec_no_qualifying_technology_is_ineligible() -> None:
    bundle = compile_tariff(ROOT, DEFINITION)
    account = _account([])
    eligibility = evaluate_eligibility(bundle, account, _dated_facts())
    assert eligibility.status == "INELIGIBLE"
    assert "QUALIFYING_TECHNOLOGY_MISSING" in eligibility.reason_codes
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(bundle, _request(account=account))
    assert raised.value.code == "TARIFF_INELIGIBLE"


def test_eelec_missing_dated_attestation_is_unknown() -> None:
    bundle = compile_tariff(ROOT, DEFINITION)
    eligibility = evaluate_eligibility(bundle, _account(), None)
    assert eligibility.status == "UNKNOWN"
    assert "DATED_TECHNOLOGY_FACT_MISSING" in eligibility.reason_codes
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(bundle, _request(omit_dated_facts=True))
    assert raised.value.code == "TARIFF_UNKNOWN"


def test_eelec_separate_technology_meter_is_ineligible() -> None:
    payload = _fixture()["dated_eligibility_facts"]
    payload["whole_house_metering"] = False
    dated_facts = DatedEligibilityFacts.model_validate_json(json.dumps(payload))
    bundle = compile_tariff(ROOT, DEFINITION)
    eligibility = evaluate_eligibility(bundle, _account(), dated_facts)
    assert eligibility.status == "INELIGIBLE"
    assert "WHOLE_HOUSE_METERING_REQUIREMENT_MISMATCH" in eligibility.reason_codes
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(
            bundle,
            _request(dated_facts=dated_facts),
        )
    assert raised.value.code == "TARIFF_INELIGIBLE"


def test_eelec_off_peak_rounding_boundary() -> None:
    local_start = datetime(2026, 7, 7, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    result = replay_compiled_tariff(
        compile_tariff(ROOT, DEFINITION),
        _request((_interval(local_start, energy_wh=14),)),
    )
    energy_line = next(
        line for line in result.line_items if line.line_item_key.endswith("off_peak")
    )
    assert Fraction(
        energy_line.pre_round_microdollars_numerator,
        energy_line.pre_round_microdollars_denominator,
    ) == Fraction(4_670_120, 1000)
    assert energy_line.rounded_cents == 0


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
def test_each_eelec_rate_or_boundary_breaks_prefrozen_golden(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    mutation(payload)
    mutated = tmp_path / "eelec.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    try:
        result = replay_compiled_tariff(compile_tariff(ROOT, mutated), _request())
    except (ReplayError, TariffCompileError):
        return
    assert [line.rounded_cents for line in result.line_items] != [
        8558,
        4839,
        15511,
        2460,
        -3618,
    ]


def test_eelec_eligibility_mutation_rejects_common_account(tmp_path: Path) -> None:
    payload = json.loads(DEFINITION.read_text(encoding="utf-8"))
    payload["eligibility_predicate"]["required_any_qualifying_technologies"] = ["HEAT_PUMP"]
    mutated = tmp_path / "eelec.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    bundle = compile_tariff(ROOT, mutated)
    eligibility = evaluate_eligibility(bundle, _account(), _dated_facts())
    assert eligibility.status == "INELIGIBLE"
    assert eligibility.reason_codes == ("QUALIFYING_TECHNOLOGY_MISSING",)
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(bundle, _request())
    assert raised.value.code == "TARIFF_INELIGIBLE"
