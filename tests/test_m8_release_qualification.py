from __future__ import annotations

from scripts.qualify_m8_release import latency_statistics


def test_latency_statistics_uses_nearest_rank_and_retains_every_sample() -> None:
    durations = [float(value) for value in range(1, 31)]
    result = latency_statistics(durations, ["a" * 64] * 30, threshold_ms=29)
    assert result["repetitions"] == 30
    assert result["durations_ms"] == durations
    assert result["p50_ms"] == 15
    assert result["p95_ms"] == 29
    assert result["p99_ms"] == 30
    assert result["passed"] is True


def test_latency_statistics_rejects_response_drift() -> None:
    result = latency_statistics(
        [10.0, 11.0],
        ["a" * 64, "b" * 64],
        threshold_ms=1_000,
    )
    assert result["deterministic_response"] is False
    assert result["passed"] is False
