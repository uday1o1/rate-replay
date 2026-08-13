import json
from pathlib import Path

import pytest
from ratereplay_tariffs.ir import ChargeIRError, e1_july_2026_ir, evaluate_compiled_ir

ROOT = Path(__file__).resolve().parents[3]


def test_e1_spike_matches_prefrozen_hand_derived_golden() -> None:
    golden = json.loads((ROOT / "tariffs/golden/e1-july-2026-complete-bill.json").read_text())
    ir = e1_july_2026_ir(baseline_wh=golden["inputs"]["baseline_wh"])
    result = evaluate_compiled_ir(
        ir,
        energy_wh=golden["inputs"]["energy_wh"],
        billing_days=golden["inputs"]["billing_days"],
    )
    assert [line.rounded_cents for line in result.lines] == golden["expected"]["line_cents"]
    assert result.total_cents == golden["expected"]["total_cents"]


def test_invalid_tier_and_overflow_fail_closed() -> None:
    ir = e1_july_2026_ir(baseline_wh=201_500)
    with pytest.raises(ChargeIRError) as overflow:
        ir.validate_bounds(maximum_energy_wh=2**63, maximum_days=31)
    assert overflow.value.code == "INT64_OVERFLOW"
