from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import scripts.qualify_m8_release as release_qualification
from scripts.qualify_m8_release import (
    ReleaseQualificationError,
    _import_reading_count,
    _job_database_result,
    _major_minor_version,
    _sql,
    _wait_job,
    latency_statistics,
)


class RecordingDeployment:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commands: list[tuple[str, ...]] = []
        self.outputs = iter(("2", "1", "EXPIRED\nSUCCEEDED"))
        self.environment: dict[str, str] = {}

    def command(self, *arguments: str) -> tuple[str, ...]:
        self.commands.append(arguments)
        return ("docker-compose", *arguments)

    def install_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(_command: tuple[str, ...], **kwargs: Any) -> SimpleNamespace:
            self.statements.append(kwargs["input"].rstrip("\n"))
            return SimpleNamespace(stdout=next(self.outputs), stderr="", returncode=0)

        monkeypatch.setattr(release_qualification.subprocess, "run", run)


def test_hardware_manifest_os_version_uses_frozen_major_minor() -> None:
    assert _major_minor_version("26.5.2") == "26.5"


def test_database_evidence_queries_use_psql_literal_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = RecordingDeployment()
    deployment.install_subprocess(monkeypatch)
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
        "SELECT attempt_count FROM jobs WHERE id=:'job_id'",
        "SELECT COUNT(*) FROM scenario_results WHERE scenario_id=:'owner_id'",
        "SELECT state FROM job_attempts WHERE job_id=:'job_id' ORDER BY attempt_number",
    ]
    assert all("--file=-" in command for command in deployment.commands)
    assert "job_id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in deployment.commands[0]
    assert "owner_id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in deployment.commands[1]


def test_import_evidence_counts_the_persisted_interval_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = RecordingDeployment()
    deployment.outputs = iter(("35040",))
    deployment.install_subprocess(monkeypatch)

    result = _import_reading_count(
        deployment,  # type: ignore[arg-type]
        import_id="b" * 32,
    )

    assert result == 35_040
    assert deployment.statements == [
        "SELECT COUNT(*) FROM interval_readings WHERE import_id=:'import_id'"
    ]
    assert "import_id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in deployment.commands[0]


def test_sql_binding_rejects_non_hex_values() -> None:
    with pytest.raises(ReleaseQualificationError, match="SQL_VARIABLE_VALUE_INVALID"):
        _sql(
            RecordingDeployment(),  # type: ignore[arg-type]
            "SELECT :'job_id'",
            variables={"job_id": "a' OR TRUE --"},
        )


def test_job_wait_reports_terminal_failure_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release_qualification,
        "_request_json",
        lambda *_args, **_kwargs: {
            "state": "FAILED",
            "failure_code": "SCENARIO_SCOPE_UNAVAILABLE",
        },
    )

    with pytest.raises(
        ReleaseQualificationError,
        match=("JOB_TERMINAL_UNEXPECTED:FAILED:" + "a" * 32 + ":SCENARIO_SCOPE_UNAVAILABLE"),
    ):
        _wait_job(
            object(),  # type: ignore[arg-type]
            "a" * 32,
            target_states=frozenset({"RUNNING"}),
            timeout_seconds=1,
            poll_seconds=0,
        )


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
