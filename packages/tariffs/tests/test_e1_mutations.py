from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from ratereplay_tariffs.billing import ReplayError, ReplayRequest, replay_compiled_tariff
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff
from ratereplay_tariffs.schema import AccountFacts, DateRange

ROOT = Path(__file__).resolve().parents[3]
DEFINITION = ROOT / "tariffs/definitions/pge-e1-2026-07.json"


def _request() -> ReplayRequest:
    return ReplayRequest(
        request_version="e1-replay-request-v1",
        profile_content_sha256="b" * 64,
        account_facts=AccountFacts(
            schema_version="account-facts-v1",
            service_window=DateRange(start=date(2026, 7, 1), end=date(2026, 8, 1)),
            service_provider="PG&E",
            service_mode="BUNDLED",
            meter_count=1,
            primary_meter_only=True,
            income_tier="TIER_3",
            care_enrolled=False,
            fera_enrolled=False,
            medical_baseline=False,
            cca_service=False,
            direct_access_service=False,
            active_bill_protection=False,
            solar_or_export=False,
            baseline_territory="T",
            baseline_quantity_code="BASIC",
            qualifying_technologies=(),
            user_attested_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
        energy_wh=310_000,
    )


def _tier_1_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["tiers"][0]["rate_microdollars_per_kwh"] += 10_000


def _tier_2_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["tiers"][1]["rate_microdollars_per_kwh"] += 10_000


def _daily_rate(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["rate_microdollars_per_day"] += 10_000


def _credit_amount(payload: dict[str, Any]) -> None:
    payload["charge_rules"][3]["amount_microdollars"] += 10_000


def _baseline_boundary(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["daily_allowance_wh"] += 1_000


def _tier_boundary(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["tiers"][0]["upper_bound_numerator"] = 2


def _credit_month_boundary(payload: dict[str, Any]) -> None:
    payload["charge_rules"][3]["applicability"]["bill_cycle_months"] = [7]


def _income_applicability(payload: dict[str, Any]) -> None:
    payload["charge_rules"][2]["applicability"]["income_tiers"] = ["TIER_2"]


def _baseline_applicability(payload: dict[str, Any]) -> None:
    payload["charge_rules"][0]["applicability"]["baseline_territories"] = ["Q"]


def _effective_date_boundary(payload: dict[str, Any]) -> None:
    payload["charge_rules"][1]["effective_range"]["start"] = "2026-07-02"


@pytest.mark.parametrize(
    "mutation",
    [
        _tier_1_rate,
        _tier_2_rate,
        _daily_rate,
        _credit_amount,
        _baseline_boundary,
        _tier_boundary,
        _credit_month_boundary,
        _income_applicability,
        _baseline_applicability,
        _effective_date_boundary,
    ],
    ids=lambda mutation: mutation.__name__.removeprefix("_"),
)
def test_each_e1_rate_or_boundary_breaks_its_golden(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    payload: dict[str, Any] = json.loads(DEFINITION.read_text(encoding="utf-8"))
    mutation(payload)
    path = tmp_path / "mutated-e1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        result = replay_compiled_tariff(compile_tariff(ROOT, path), _request())
    except (TariffCompileError, ReplayError):
        return
    assert [line.rounded_cents for line in result.line_items] != [
        6561,
        4416,
        2460,
        -3618,
    ] or result.manifest.baseline_allowance_wh != 201_500
