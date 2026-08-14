from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from scripts.qualify_m8_release import (
    _job_database_result,
    _major_minor_version,
    latency_statistics,
)


class RecordingDeployment:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.outputs = iter(("2", "1", "EXPIRED\nSUCCEEDED"))

    def run(self, *arguments: str, **_kwargs: Any) -> SimpleNamespace:
        self.statements.append(arguments[-1])
        return SimpleNamespace(stdout=next(self.outputs))


def test_hardware_manifest_os_version_uses_frozen_major_minor() -> None:
    assert _major_minor_version("26.5.2") == "26.5"


def test_database_evidence_queries_use_validated_sql_literals() -> None:
    deployment = RecordingDeployment()
    job_id = "a" * 32
    owner_id = "b" * 32

    result = _job_database_result(
        deployment,  # type: ignore[arg-type]
        job_id=job_id,
        result_table="scenario_results",
        result_owner_column="scenario_id",
        result_owner_id=owner_id,
    )

    assert result == (2, 1, ["EXPIRED", "SUCCEEDED"])
    assert deployment.statements == [
        "SELECT attempt_count FROM jobs WHERE id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
        "SELECT COUNT(*) FROM scenario_results "
        "WHERE scenario_id='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
        "SELECT state FROM job_attempts "
        "WHERE job_id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ORDER BY attempt_number",
    ]


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
