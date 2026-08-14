from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from ratereplay_tariffs.admission import AdmittedTariff, load_all_admitted_tariffs
from ratereplay_tariffs.billing import IntervalReplayRequest, ReplayInterval, UserUnsupportedLine
from ratereplay_tariffs.comparison import (
    ComparisonError,
    ComparisonResult,
    compare_admitted_tariffs,
)
from ratereplay_tariffs.compiler import TariffCompileError, compile_tariff
from ratereplay_tariffs.schema import AccountFacts, ChargeComponentKey, DatedEligibilityFacts

ROOT = Path(__file__).resolve().parents[3]
REQUIRED_COMPONENTS = cast(
    tuple[ChargeComponentKey, ...],
    (
        "base_services_charge",
        "baseline_adjustment",
        "bundled_energy",
        "california_climate_credit",
        "minimum_bill_adjustment",
    ),
)


def _comparison_facts() -> tuple[AccountFacts, DatedEligibilityFacts]:
    payload = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
        ),
    )
    return (
        AccountFacts.model_validate_json(json.dumps(payload["account_facts"])),
        DatedEligibilityFacts.model_validate_json(json.dumps(payload["dated_eligibility_facts"])),
    )


def _request(
    *,
    dated_updates: dict[str, object] | None = None,
    current_bill_total_cents: int | None = None,
    unsupported_lines: tuple[UserUnsupportedLine, ...] = (),
) -> IntervalReplayRequest:
    profile = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "data/demo/july-2026-simulated-profile.json").read_text(encoding="utf-8")
        ),
    )
    account, dated = _comparison_facts()
    if dated_updates:
        payload = dated.model_dump(mode="json")
        payload.update(dated_updates)
        dated = DatedEligibilityFacts.model_validate_json(json.dumps(payload))
    readings = cast(list[dict[str, Any]], profile["readings"])
    intervals = tuple(
        ReplayInterval(
            start_utc_ns=int(
                datetime.fromisoformat(
                    cast(str, reading["start_utc"]).replace("Z", "+00:00")
                ).timestamp()
                * 1_000_000_000
            ),
            duration_seconds=cast(int, reading["duration_seconds"]),
            energy_wh=cast(int, reading["energy_wh"]),
        )
        for reading in readings
    )
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256=("47b449f47039960cde24666a5ed2723781b7773d624dbdd2b74de78e02da19ce"),
        account_facts=account,
        energy_wh=cast(int, profile["total_energy_wh"]),
        intervals=intervals,
        dated_eligibility_facts=dated,
        current_bill_total_cents=current_bill_total_cents,
        user_unsupported_lines=unsupported_lines,
    )


def _compare(
    request: IntervalReplayRequest,
    tariffs: tuple[AdmittedTariff, ...] | None = None,
) -> ComparisonResult:
    return compare_admitted_tariffs(
        tariffs or load_all_admitted_tariffs(ROOT),
        request,
        current_tariff_version_id="pge-e1-2026-07",
        required_component_keys=REQUIRED_COMPONENTS,
    )


def test_all_admitted_tariffs_produce_rankable_comparison() -> None:
    result = _compare(_request())
    assert result.rankable is True
    assert result.exclusions == ()
    assert result.ranked_tariff_version_ids == (
        "pge-etoud-2026-07",
        "pge-ev2a-2026-07",
        "pge-e1-2026-07",
        "pge-etouc-2026-07",
        "pge-eelec-2026-07",
    )
    assert result.winner_tariff_version_ids == ("pge-etoud-2026-07",)
    assert result.savings_against_current_supported_cents == 1707
    assert result.common_supported_component_keys == (
        "base_services_charge",
        "bundled_energy",
        "california_climate_credit",
    )
    costs = {
        candidate.plan_code: candidate.alternative_plan.supported_calculated_cents
        for candidate in result.candidates
        if candidate.alternative_plan is not None
    }
    assert costs == {
        "E-1": 27728,
        "E-ELEC": 30278,
        "E-TOU-C": 30253,
        "E-TOU-D": 26021,
        "EV2-A": 26890,
    }
    etouc = next(candidate for candidate in result.candidates if candidate.plan_code == "E-TOU-C")
    baseline = next(
        item for item in etouc.component_coverage if item.component_key == "baseline_adjustment"
    )
    assert baseline.status == "SUPPORTED"
    assert baseline.contributing_rule_ids == ("ETOUC_SUMMER_TOTAL_RATES_2026_06_01",)
    assert all(
        candidate.alternative_plan is not None
        and candidate.alternative_plan.tariff_unsupported_placeholders == ()
        for candidate in result.candidates
    )


def test_candidate_input_order_does_not_change_comparison_identity() -> None:
    tariffs = load_all_admitted_tariffs(ROOT)
    assert (
        _compare(_request(), tariffs).comparison_sha256
        == _compare(_request(), tuple(reversed(tariffs))).comparison_sha256
    )


@pytest.mark.parametrize(
    ("annual_usage_wh", "status", "exclusion_code"),
    [
        (None, "UNKNOWN", "CANDIDATE_ELIGIBILITY_UNKNOWN"),
        (16_000_001, "INELIGIBLE", "CANDIDATE_INELIGIBLE"),
    ],
)
def test_ev2a_unknown_or_ineligible_facts_block_ranking(
    annual_usage_wh: int | None, status: str, exclusion_code: str
) -> None:
    result = _compare(_request(dated_updates={"annual_usage_wh": annual_usage_wh}))
    ev2a = next(candidate for candidate in result.candidates if candidate.plan_code == "EV2-A")
    assert ev2a.eligibility.status == status
    assert ev2a.alternative_plan is None
    assert result.rankable is False
    assert result.ranked_tariff_version_ids == ()
    assert result.winner_tariff_version_ids == ()
    assert result.savings_against_current_supported_cents is None
    assert any(
        item.code == exclusion_code and item.tariff_version_id == "pge-ev2a-2026-07"
        for item in result.exclusions
    )


def test_unclassified_active_component_blocks_ranking() -> None:
    tariffs = list(load_all_admitted_tariffs(ROOT))
    index = next(index for index, item in enumerate(tariffs) if item.lock.plan_code == "E-TOU-C")
    admitted = tariffs[index]
    normalized = dict(admitted.compilation.normalized_ast)
    normalized["comparison_component_keys"] = [
        "bundled_energy",
        "base_services_charge",
        "california_climate_credit",
    ]
    compilation = admitted.compilation.model_copy(update={"normalized_ast": normalized})
    tariffs[index] = admitted.model_copy(update={"compilation": compilation})

    result = _compare(_request(), tuple(tariffs))
    assert result.rankable is False
    assert result.savings_against_current_supported_cents is None
    assert any(
        item.code == "UNCLASSIFIED_ACTIVE_COMPONENT"
        and item.tariff_version_id == "pge-etouc-2026-07"
        and item.component_key == "baseline_adjustment"
        for item in result.exclusions
    )


def test_eligibility_rule_mutation_blocks_ranking(tmp_path: Path) -> None:
    definition = ROOT / "tariffs/definitions/pge-ev2a-2026-07.json"
    payload = json.loads(definition.read_text(encoding="utf-8"))
    payload["eligibility_predicate"]["maximum_annual_baseline_ratio_numerator"] = 2
    mutated_path = tmp_path / "ev2a.json"
    mutated_path.write_text(json.dumps(payload), encoding="utf-8")
    mutated_bundle = compile_tariff(ROOT, mutated_path)
    tariffs = list(load_all_admitted_tariffs(ROOT))
    index = next(index for index, item in enumerate(tariffs) if item.lock.plan_code == "EV2-A")
    tariffs[index] = tariffs[index].model_copy(update={"compilation": mutated_bundle})

    result = _compare(_request(), tuple(tariffs))
    assert result.rankable is False
    assert result.savings_against_current_supported_cents is None
    assert any(item.code == "CANDIDATE_INELIGIBLE" for item in result.exclusions)


def test_compiler_rejects_comparison_component_rule_mutation(tmp_path: Path) -> None:
    definition = ROOT / "tariffs/definitions/pge-etouc-2026-07.json"
    payload = json.loads(definition.read_text(encoding="utf-8"))
    payload["comparison_component_keys"].remove("baseline_adjustment")
    mutated = tmp_path / "etouc.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TariffCompileError) as raised:
        compile_tariff(ROOT, mutated)
    assert raised.value.code == "COMPARISON_COMPONENT_COVERAGE_MISMATCH"


def test_alternative_plan_rejects_current_bill_inputs() -> None:
    request = _request(
        current_bill_total_cents=30_000,
        unsupported_lines=(
            UserUnsupportedLine(
                line_item_key="tax",
                description="Current bill only",
                amount_cents=100,
            ),
        ),
    )
    with pytest.raises(ComparisonError) as raised:
        _compare(request)
    assert raised.value.code == "ALTERNATIVE_RECONCILIATION_FORBIDDEN"


def test_comparison_candidate_contract_rejects_duplicates_and_missing_current() -> None:
    tariffs = load_all_admitted_tariffs(ROOT)
    with pytest.raises(ComparisonError) as duplicate:
        _compare(_request(), (tariffs[0], tariffs[0]))
    assert duplicate.value.code == "DUPLICATE_CANDIDATE"
    with pytest.raises(ComparisonError) as missing:
        _compare(_request(), tariffs[1:])
    assert missing.value.code == "CURRENT_TARIFF_NOT_CANDIDATE"
