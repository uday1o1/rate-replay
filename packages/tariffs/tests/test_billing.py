from __future__ import annotations

import json
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ratereplay_tariffs.billing import (
    ReconciliationPolicy,
    ReplayError,
    ReplayRequest,
    UserUnsupportedLine,
    evaluate_eligibility,
    replay_compiled_tariff,
)
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.ir import round_half_up_cents
from ratereplay_tariffs.schema import AccountFacts, DateRange

ROOT = Path(__file__).resolve().parents[3]


def _facts(**changes: object) -> AccountFacts:
    values: dict[str, object] = {
        "schema_version": "account-facts-v1",
        "service_window": DateRange(
            start=datetime(2026, 7, 1, tzinfo=UTC).date(),
            end=datetime(2026, 8, 1, tzinfo=UTC).date(),
        ),
        "service_provider": "PG&E",
        "service_mode": "BUNDLED",
        "meter_count": 1,
        "primary_meter_only": True,
        "income_tier": "TIER_3",
        "care_enrolled": False,
        "fera_enrolled": False,
        "medical_baseline": False,
        "cca_service": False,
        "direct_access_service": False,
        "active_bill_protection": False,
        "solar_or_export": False,
        "baseline_territory": "T",
        "baseline_quantity_code": "BASIC",
        "qualifying_technologies": (),
        "user_attested_at": datetime(2026, 7, 1, tzinfo=UTC),
    }
    values.update(changes)
    return AccountFacts.model_validate(values)


def _request(
    energy_wh: int = 310_000,
    *,
    current_total: int | None = None,
    unsupported: tuple[UserUnsupportedLine, ...] = (),
    facts: AccountFacts | None = None,
) -> ReplayRequest:
    return ReplayRequest(
        request_version="e1-replay-request-v1",
        profile_content_sha256="a" * 64,
        account_facts=facts or _facts(),
        energy_wh=energy_wh,
        current_bill_total_cents=current_total,
        user_unsupported_lines=unsupported,
    )


def test_complete_bill_matches_prefrozen_golden_with_provenance() -> None:
    golden = json.loads(
        (ROOT / "tariffs/golden/e1-july-2026-complete-bill.json").read_text(encoding="utf-8")
    )
    result = replay_compiled_tariff(compile_tariff(ROOT), _request())

    assert [line.rounded_cents for line in result.line_items] == golden["expected"]["line_cents"]
    assert result.supported_calculated_cents == golden["expected"]["total_cents"]
    assert result.manifest.baseline_allowance_wh == golden["inputs"]["baseline_wh"]
    assert result.manifest.billing_days == golden["inputs"]["billing_days"]
    assert result.manifest.bill_cycle_month == golden["inputs"]["bill_cycle_month"]
    assert {line.rule_id for line in result.line_items} <= set(golden["rule_ids"])
    assert all(line.source_id in golden["source_ids"] for line in result.line_items)
    assert sum(line.rounded_cents for line in result.line_items) == (
        result.supported_calculated_cents
    )


def test_rule_boundary_goldens() -> None:
    suite = json.loads(
        (ROOT / "tariffs/golden/e1-july-2026-boundaries.json").read_text(encoding="utf-8")
    )
    by_id = {case["case_id"]: case for case in suite["cases"]}
    compiled = compile_tariff(ROOT)

    baseline_case = by_id["territory-t-basic-summer-baseline-31-days"]
    baseline_result = replay_compiled_tariff(compiled, _request(0))
    assert (
        baseline_result.manifest.baseline_allowance_wh
        == baseline_case["expected"]["billing_period_allowance_wh"]
    )

    at_boundary = replay_compiled_tariff(compiled, _request(201_500))
    over_boundary = replay_compiled_tariff(compiled, _request(201_501))
    at_energy = [
        line for line in at_boundary.line_items if line.line_item_key.startswith("bundled")
    ]
    over_energy = [
        line for line in over_boundary.line_items if line.line_item_key.startswith("bundled")
    ]
    assert [line.quantity_numerator for line in at_energy] == [201_500]
    assert [line.quantity_numerator for line in over_energy] == [201_500, 1]

    below = replay_compiled_tariff(compiled, _request(15))
    above = replay_compiled_tariff(compiled, _request(16))
    assert (
        below.line_items[0].rounded_cents
        == by_id["tier-1-rounds-down-below-half-cent"]["expected"]["rounded_cents"]
    )
    assert (
        above.line_items[0].rounded_cents
        == by_id["tier-1-rounds-up-above-half-cent"]["expected"]["rounded_cents"]
    )

    fixed = by_id["one-day-base-service-rounding"]
    assert round_half_up_cents(Fraction(793_430)) == fixed["expected"]["rounded_cents"]
    assert evaluate_eligibility(compiled, _facts()).status == "ELIGIBLE"
    assert evaluate_eligibility(compiled, _facts(income_tier="TIER_1")).model_dump()[
        "reason_codes"
    ] == ("UNSUPPORTED_INCOME_TIER",)


def test_reconciliation_keeps_unsupported_items_and_residual_visible() -> None:
    unsupported = (
        UserUnsupportedLine(
            line_item_key="local_tax", description="Entered from current bill", amount_cents=200
        ),
    )
    result = replay_compiled_tariff(
        compile_tariff(ROOT), _request(current_total=11_000, unsupported=unsupported)
    )

    assert result.user_unsupported_lines == unsupported
    assert result.reconciliation is not None
    assert result.reconciliation.supported_calculated_cents == 9_819
    assert result.reconciliation.user_unsupported_cents == 200
    assert result.reconciliation.unexplained_residual_cents == 981
    assert result.reconciliation.entered_bill_total_cents == 11_000
    assert result.reconciliation.classification == "REVIEW_REQUIRED"
    assert result.manifest.reconciliation_input_sha256 == result.reconciliation.input_sha256
    assert result.manifest.reconciliation_policy_sha256 == result.reconciliation.policy_sha256


def test_reconciliation_hashes_every_difference_making_input() -> None:
    compiled = compile_tariff(ROOT)
    first = replay_compiled_tariff(compiled, _request(current_total=10_000))
    changed_total = replay_compiled_tariff(compiled, _request(current_total=10_001))
    changed_policy = replay_compiled_tariff(
        compiled,
        _request(current_total=10_000),
        policy=ReconciliationPolicy(review_tolerance_cents=99),
    )

    assert first.manifest.reconciliation_input_sha256 != (
        changed_total.manifest.reconciliation_input_sha256
    )
    assert first.manifest.calculation_sha256 != changed_total.manifest.calculation_sha256
    assert first.manifest.reconciliation_policy_sha256 != (
        changed_policy.manifest.reconciliation_policy_sha256
    )
    assert first.manifest.calculation_sha256 != changed_policy.manifest.calculation_sha256


def test_user_unsupported_line_requires_current_total() -> None:
    request = _request(
        unsupported=(UserUnsupportedLine(line_item_key="tax", description="A tax", amount_cents=1),)
    )
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(compile_tariff(ROOT), request)
    assert raised.value.code == "UNSUPPORTED_LINES_REQUIRE_BILL_TOTAL"


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (_facts(income_tier="TIER_1"), "UNSUPPORTED_INCOME_TIER"),
        (_facts(baseline_territory="Q"), "UNSUPPORTED_BASELINE_CONFIGURATION"),
    ],
)
def test_unknown_eligibility_never_replays(facts: AccountFacts, reason: str) -> None:
    compiled = compile_tariff(ROOT)
    eligibility = evaluate_eligibility(compiled, facts)
    assert eligibility.status == "UNKNOWN"
    assert reason in eligibility.reason_codes
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(compiled, _request(facts=facts))
    assert raised.value.code == "TARIFF_UNKNOWN"


def test_ineligible_account_never_replays() -> None:
    facts = _facts(cca_service=True)
    eligibility = evaluate_eligibility(compile_tariff(ROOT), facts)
    assert eligibility.status == "INELIGIBLE"
    assert eligibility.reason_codes == ("CCA_STATUS_MISMATCH",)


def test_compiled_bounds_fail_closed() -> None:
    with pytest.raises(ReplayError) as raised:
        replay_compiled_tariff(compile_tariff(ROOT), _request(100_000_001))
    assert raised.value.code == "ENERGY_BOUND_EXCEEDED"


@given(st.integers(min_value=0, max_value=1_000_000))
def test_charge_total_always_equals_emitted_lines(energy_wh: int) -> None:
    result = replay_compiled_tariff(compile_tariff(ROOT), _request(energy_wh))
    assert result.supported_calculated_cents == sum(
        line.rounded_cents for line in result.line_items
    )


def test_replay_result_hash_is_stable() -> None:
    compiled = compile_tariff(ROOT)
    request = _request(current_total=10_000)
    assert replay_compiled_tariff(compiled, request) == replay_compiled_tariff(compiled, request)
