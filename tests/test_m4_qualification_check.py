from __future__ import annotations

from scripts.qualify_m4 import qualify


def test_m4_qualification_can_write_without_mutating_historical_evidence(tmp_path) -> None:
    output = tmp_path / "m4-optimizer-qualification.json"
    result = qualify(output)
    assert output.is_file()
    assert result["gate_result"] == "PASS"
    assert result["portfolio_scenario"]["exact_search_status"] == "OPTIMAL"
    assert result["independent_exhaustive_oracle"]["returned_schedule_in_optimum_set"] is True
