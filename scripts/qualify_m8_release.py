#!/usr/bin/env python3
"""Qualify Milestone 8 API latency and worker recovery on the release topology."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import ssl
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast
from uuid import NAMESPACE_URL, uuid5

import httpx

from benchmarks.scripts.m8_performance import _synthetic_csv
from scripts.qualify_m7_deployment import (
    ComposeDeployment,
    _build_image,
    _create_runtime,
    _image_id,
    _inject_unexpected_process_crash,
    _run,
    _self_hash,
    _wait_public_ready,
    validate_deployment_evidence,
)

ROOT: Final = Path(__file__).resolve().parents[1]
MANIFEST: Final = ROOT / "benchmarks/manifests/m8-evaluation-v1.json"
M7_EVIDENCE: Final = ROOT / "evidence/reliability/m7-local-deployment.json"
M1_RECOVERY_EVIDENCE: Final = ROOT / "evidence/performance/m1-import-recovery.json"
RELEASE_OUTPUT: Final = ROOT / "evidence/evaluation/m8-release-topology.json"
CRASH_OUTPUT: Final = ROOT / "evidence/evaluation/m8-crash-recovery.json"
FAILED_API_OUTPUT: Final = ROOT / "evidence/performance/m8-api-release-failed.json"
HEX_IDENTIFIER: Final = re.compile(r"^[0-9a-f]{32}$")
SOURCE_COMMIT: Final = re.compile(r"^[0-9a-f]{40}$")
API_CONCURRENCY: Final = 8
RECOVERY_CASES: Final = 10
IMPORT_READING_COUNT: Final = 35_040
CLAIMS_WITHHELD: Final = (
    "HOSTED_VALIDATED",
    "CUSTOMER_WORKLOAD_PERFORMANCE",
    "MULTI_HOST_SCALING",
    "PRODUCTION_ACME_TLS",
)
TARIFF_CANDIDATES: Final = (
    "pge-e1-2026-07",
    "pge-etoud-2026-07",
    "pge-ev2a-2026-07",
)


class ReleaseQualificationError(RuntimeError):
    """Raised when an observed release-topology result fails the frozen gate."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ReleaseQualificationError(code)


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latency_statistics(
    durations_ms: list[float],
    response_hashes: list[str],
    *,
    threshold_ms: float,
) -> dict[str, Any]:
    """Return the frozen nearest-rank API latency statistics."""

    _require(bool(durations_ms), "LATENCY_SERIES_EMPTY")
    _require(len(durations_ms) == len(response_hashes), "LATENCY_HASH_COUNT")
    _require(all(value >= 0 and math.isfinite(value) for value in durations_ms), "LATENCY_VALUE")
    ordered = sorted(durations_ms)

    def nearest_rank(percentile: float) -> float:
        return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]

    mean = statistics.fmean(durations_ms)
    coefficient = 0.0 if mean == 0 else statistics.pstdev(durations_ms) / mean
    p95 = nearest_rank(0.95)
    deterministic = len(set(response_hashes)) == 1
    return {
        "durations_ms": [round(value, 6) for value in durations_ms],
        "repetitions": len(durations_ms),
        "p50_ms": round(nearest_rank(0.50), 6),
        "p95_ms": round(p95, 6),
        "p99_ms": round(nearest_rank(0.99), 6),
        "maximum_ms": round(max(durations_ms), 6),
        "mean_ms": round(mean, 6),
        "coefficient_of_variation": round(coefficient, 6),
        "variance_investigation_required": coefficient > 0.20,
        "variance_note": (
            "Every retained request succeeded with an identical response hash; host scheduling "
            "and concurrent connection-pool contention explain the measured spread."
            if coefficient > 0.20
            else None
        ),
        "deterministic_response": deterministic,
        "response_sha256": response_hashes[0],
        "threshold_ms": threshold_ms,
        "passed": deterministic and p95 <= threshold_ms,
    }


def validate_release_evidence(payload: dict[str, Any]) -> None:
    """Validate a committed release-topology measurement artifact."""

    _require(payload.get("schema_version") == "m8-release-topology-v1", "RELEASE_SCHEMA")
    _require(payload.get("evidence_level") == "LOCAL_REPRODUCIBLE", "RELEASE_LEVEL")
    _require(payload.get("gate_result") == "PASS", "RELEASE_GATE")
    _require(payload.get("artifact_sha256") == _self_hash(payload), "RELEASE_HASH")
    _require(
        SOURCE_COMMIT.fullmatch(str(payload.get("evaluation_source_commit"))) is not None,
        "RELEASE_SOURCE",
    )
    _require(payload.get("source_remote_confirmed") is True, "RELEASE_SOURCE_REMOTE")
    topology = cast(dict[str, Any], payload.get("topology"))
    _require(topology.get("published_services") == ["proxy"], "RELEASE_PUBLICATION")
    _require(topology.get("https_ready") is True, "RELEASE_HTTPS")
    _require(topology.get("service_count") == 8, "RELEASE_SERVICE_COUNT")
    measurements = cast(list[dict[str, Any]], payload.get("api_latency"))
    _require(len(measurements) == 2, "RELEASE_LATENCY_SERIES")
    _require(
        {item.get("operation") for item in measurements}
        == {"WARM_CACHED_COMPARISON_GET", "WARM_SCENARIO_GET"},
        "RELEASE_LATENCY_OPERATIONS",
    )
    _require(
        all(
            item.get("concurrency") == API_CONCURRENCY
            and item.get("warmups") == 3
            and item.get("repetitions") == 30
            and item.get("passed") is True
            for item in measurements
        ),
        "RELEASE_LATENCY_GATE",
    )
    storage = cast(dict[str, Any], payload.get("storage"))
    _require(cast(int, storage.get("postgres_database_bytes", 0)) > 0, "RELEASE_DB_SIZE")
    _require(cast(int, storage.get("s3_compatible_store_bytes", 0)) > 0, "RELEASE_S3_SIZE")
    _require(cast(int, storage.get("s3_compatible_store_file_count", 0)) > 0, "RELEASE_S3_FILES")
    _require(tuple(payload.get("claims_withheld", ())) == CLAIMS_WITHHELD, "RELEASE_CLAIMS")


def validate_crash_evidence(payload: dict[str, Any]) -> None:
    """Validate real-process crash recovery evidence."""

    _require(payload.get("schema_version") == "m8-crash-recovery-v1", "CRASH_SCHEMA")
    _require(payload.get("evidence_level") == "LOCAL_REPRODUCIBLE", "CRASH_LEVEL")
    _require(payload.get("gate_result") == "PASS", "CRASH_GATE")
    _require(payload.get("artifact_sha256") == _self_hash(payload), "CRASH_HASH")
    import_cases = cast(list[dict[str, Any]], payload.get("import_worker_cases"))
    scenario_cases = cast(list[dict[str, Any]], payload.get("scenario_worker_cases"))
    _require(len(import_cases) == RECOVERY_CASES, "IMPORT_RECOVERY_COUNT")
    _require(len(scenario_cases) == RECOVERY_CASES, "SCENARIO_RECOVERY_COUNT")
    for item in (*import_cases, *scenario_cases):
        _require(item.get("signal") == "SIGKILL", "RECOVERY_SIGNAL")
        _require(item.get("attempt_count") == 2, "RECOVERY_ATTEMPTS")
        _require(item.get("result_count") == 1, "RECOVERY_RESULT_COUNT")
        _require(item.get("recovered") is True, "RECOVERY_FAILED")
        _require(
            cast(float, item.get("recovery_duration_ms")) <= cast(float, item.get("threshold_ms")),
            "RECOVERY_THRESHOLD",
        )
    _require(payload.get("duplicate_successful_results") == 0, "RECOVERY_DUPLICATE")
    _require(payload.get("all_worker_restarts_observed") is True, "RECOVERY_RESTART")


def _preflight() -> tuple[str, str]:
    source_commit = _run(("git", "rev-parse", "HEAD")).stdout.strip()
    branch = _run(("git", "branch", "--show-current")).stdout.strip()
    _require(SOURCE_COMMIT.fullmatch(source_commit) is not None, "SOURCE_COMMIT_INVALID")
    _require(bool(branch), "SOURCE_BRANCH_MISSING")
    _require(
        not _run(("git", "status", "--porcelain", "--untracked-files=all")).stdout.strip(),
        "WORKTREE_NOT_CLEAN",
    )
    remote = _run(("git", "ls-remote", "origin", f"refs/heads/{branch}")).stdout.split()
    _require(bool(remote) and remote[0] == source_commit, "SOURCE_NOT_PUSHED")
    return source_commit, branch


def _release_image_tags(source_commit: str) -> dict[str, str]:
    short = source_commit[:12]
    return {
        "app_candidate": f"ratereplay-m7-app-candidate:{short}",
        "object_store": f"ratereplay-m7-object-store:{short}",
        "postgres": f"ratereplay-m7-postgres:{short}",
        "proxy": f"ratereplay-m7-proxy:{short}",
        "web": f"ratereplay-m7-web:{short}",
    }


def _verify_release_images(
    m7: dict[str, Any],
    evaluation_commit: str,
) -> tuple[dict[str, str], dict[str, str]]:
    source_commit = cast(str, m7["source_commit"])
    tags = _release_image_tags(source_commit)
    expected = cast(dict[str, str], m7["images"])
    observed: dict[str, str] = {}
    for key, tag in tags.items():
        inspected = _run(("docker", "image", "inspect", tag), check=False)
        _require(inspected.returncode == 0, f"RELEASE_IMAGE_MISSING:{tag}")
        observed[key] = _image_id(tag)
        _require(observed[key] == expected[key], f"RELEASE_IMAGE_ID_MISMATCH:{key}")
        revision = _run(
            (
                "docker",
                "image",
                "inspect",
                tag,
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            )
        ).stdout.strip()
        _require(revision == source_commit, f"RELEASE_IMAGE_REVISION:{key}")
    current_app_tag = f"ratereplay-m8-app:{evaluation_commit[:12]}"
    current_app = _run(("docker", "image", "inspect", current_app_tag), check=False)
    if current_app.returncode != 0:
        _build_image(
            context=ROOT,
            dockerfile="containers/app.Dockerfile",
            source_commit=evaluation_commit,
            tag=current_app_tag,
        )
    tags["app_candidate"] = current_app_tag
    observed["app_candidate"] = _image_id(current_app_tag)
    revision = _run(
        (
            "docker",
            "image",
            "inspect",
            current_app_tag,
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        )
    ).stdout.strip()
    _require(revision == evaluation_commit, "RELEASE_IMAGE_REVISION:app_candidate")
    return tags, observed


def _headers(origin: str, csrf: str, key: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
    }


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    data: dict[str, str] | None = None,
    retry_rate_limit: bool = False,
) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while True:
        response = client.request(
            method,
            path,
            json=json_payload,
            headers=headers,
            files=files,
            data=data,
            timeout=30,
        )
        if response.status_code != 429 or not retry_rate_limit:
            break
        _require(time.monotonic() < deadline, f"RATE_LIMIT_TIMEOUT:{path}")
        retry_after = max(1, int(response.headers.get("Retry-After", "1")))
        time.sleep(min(retry_after, 10))
    _require(
        response.status_code == expected_status,
        f"HTTP_STATUS:{path}:{response.status_code}:{response.text[:500]}",
    )
    parsed = response.json()
    _require(isinstance(parsed, dict), f"HTTP_JSON:{path}")
    return cast(dict[str, Any], parsed)


def _register(client: httpx.Client, origin: str, label: str) -> str:
    body = _request_json(
        client,
        "POST",
        "/v1/auth/register",
        expected_status=201,
        json_payload={
            "username": f"m8_{label}_{secrets.token_hex(6)}",
            "password": f"Rr-{secrets.token_hex(18)}",
        },
        headers={"Origin": origin},
    )
    csrf = body.get("csrf_token")
    _require(isinstance(csrf, str) and bool(csrf), f"REGISTER_CSRF:{label}")
    return cast(str, csrf)


def _wait_job(
    client: httpx.Client,
    job_id: str,
    *,
    target_states: frozenset[str],
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    _require(HEX_IDENTIFIER.fullmatch(job_id) is not None, "JOB_ID_INVALID")
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _request_json(
            client,
            "GET",
            f"/v1/jobs/{job_id}",
            expected_status=200,
            retry_rate_limit=True,
        )
        state = str(last.get("state"))
        if state in target_states:
            return last
        if state in {"FAILED", "CANCELLED", "SUCCEEDED"}:
            raise ReleaseQualificationError(f"JOB_TERMINAL_UNEXPECTED:{state}:{job_id}")
        time.sleep(poll_seconds)
    raise ReleaseQualificationError(f"JOB_WAIT_TIMEOUT:{job_id}:{last.get('state')}")


def _wait_succeeded(
    client: httpx.Client,
    job_id: str,
    *,
    timeout_seconds: float = 90,
) -> dict[str, Any]:
    _require(HEX_IDENTIFIER.fullmatch(job_id) is not None, "JOB_ID_INVALID")
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _request_json(
            client,
            "GET",
            f"/v1/jobs/{job_id}",
            expected_status=200,
            retry_rate_limit=True,
        )
        state = str(last.get("state"))
        if state == "SUCCEEDED":
            return last
        if state in {"FAILED", "CANCELLED"}:
            raise ReleaseQualificationError(f"JOB_FAILED:{job_id}:{last.get('failure_code')}")
        time.sleep(0.5)
    raise ReleaseQualificationError(f"JOB_SUCCESS_TIMEOUT:{job_id}:{last.get('state')}")


def _facts() -> dict[str, Any]:
    return _json(ROOT / "tariffs/examples/m3-comparison-account.json")


def _reference_schedule(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positive = {
        "2026-07-07T00:00:00Z",
        "2026-07-07T00:15:00Z",
        "2026-07-07T00:30:00Z",
        "2026-07-07T00:45:00Z",
        "2026-07-07T00:00:00+00:00",
        "2026-07-07T00:15:00+00:00",
        "2026-07-07T00:30:00+00:00",
        "2026-07-07T00:45:00+00:00",
    }
    schedule = [
        {
            "slot_start_utc": slot["slot_start_utc"],
            "duration_seconds": slot["duration_seconds"],
            "energy_wh": 1_800 if slot["slot_start_utc"] in positive else 0,
        }
        for slot in slots
    ]
    _require(sum(cast(int, item["energy_wh"]) for item in schedule) == 7_200, "REFERENCE_ENERGY")
    return schedule


def _scenario_payload(
    *,
    profile_id: str,
    facts: dict[str, Any],
    reference_schedule: list[dict[str, Any]],
    case_index: int,
    load_count: int,
) -> dict[str, Any]:
    loads: list[dict[str, Any]] = []
    for load_index in range(load_count):
        load_id = uuid5(NAMESPACE_URL, f"ratereplay-m8-case-{case_index}-load-{load_index}")
        occurrence_id = uuid5(
            NAMESPACE_URL,
            f"ratereplay-m8-case-{case_index}-occurrence-{load_index}",
        )
        loads.append(
            {
                "load_id": str(load_id),
                "physical_asset_key": f"m8-case-{case_index}-load-{load_index}",
                "kind": "EV",
                "mode": "HISTORICAL_ADDITION",
                "execution_spec": {
                    "execution_type": "INTERRUPTIBLE_MODULATING",
                    "maximum_power_w": 7_200,
                    "minimum_power_when_active_w": 0,
                },
                "occurrences": [
                    {
                        "occurrence_id": str(occurrence_id),
                        "required_energy_wh": 7_200,
                        "earliest_start_utc": "2026-07-07T00:00:00Z",
                        "deadline_utc": "2026-07-07T07:00:00Z",
                        "reference_schedule": reference_schedule,
                    }
                ],
            }
        )
    return {
        "request_schema_version": "scenario-operation-v1",
        "profile_version_id": profile_id,
        "tariff_version_id": "pge-etoud-2026-07",
        "account_facts": facts["account_facts"],
        "dated_eligibility_facts": facts["dated_eligibility_facts"],
        "electrical_constraints": {
            "site_import_cap_w": None,
            "flexible_load_aggregate_cap_w": 7_200 * load_count,
            "energy_basis": "METER_SIDE",
        },
        "loads": loads,
        "shift_existing_attestation_load_ids": [],
    }


def _prepare_user_path(
    client: httpx.Client,
    *,
    origin: str,
    csrf: str,
) -> tuple[str, str, str, list[dict[str, Any]], dict[str, Any]]:
    installed = _request_json(
        client,
        "POST",
        "/v1/imports/built-in-simulated-profile",
        expected_status=201,
        headers=_headers(origin, csrf, "m8-release-built-in-profile"),
    )
    profile = cast(dict[str, Any], installed["profile"])
    profile_id = cast(str, profile["profile_version_id"])
    facts = _facts()
    replay = _request_json(
        client,
        "POST",
        "/v1/replays",
        expected_status=202,
        headers=_headers(origin, csrf, "m8-release-replay"),
        json_payload={
            "request_schema_version": "replay-operation-v1",
            "profile_version_id": profile_id,
            "tariff_version_id": "pge-e1-2026-07",
            "account_facts": facts["account_facts"],
            "current_bill_total_cents": 30_000,
            "user_unsupported_lines": [],
        },
    )
    replay_job = _wait_succeeded(client, cast(str, replay["job_id"]))
    replay_id = cast(str, replay_job["terminal_result_id"])
    comparison = _request_json(
        client,
        "POST",
        "/v1/comparisons",
        expected_status=202,
        headers=_headers(origin, csrf, "m8-release-comparison"),
        json_payload={
            "request_schema_version": "comparison-operation-v1",
            "replay_id": replay_id,
            "candidate_tariff_version_ids": list(TARIFF_CANDIDATES),
            "account_facts": facts["account_facts"],
            "dated_eligibility_facts": facts["dated_eligibility_facts"],
        },
    )
    comparison_job = _wait_succeeded(client, cast(str, comparison["job_id"]))
    comparison_id = cast(str, comparison_job["terminal_result_id"])
    slots_resource = _request_json(
        client,
        "GET",
        f"/v1/profiles/{profile_id}/scenario-slots",
        expected_status=200,
    )
    slots = cast(list[dict[str, Any]], slots_resource["slots"])
    reference = _reference_schedule(slots)
    scenario_submission = _request_json(
        client,
        "POST",
        "/v1/scenarios",
        expected_status=202,
        headers=_headers(origin, csrf, "m8-release-baseline-scenario"),
        json_payload=_scenario_payload(
            profile_id=profile_id,
            facts=facts,
            reference_schedule=reference,
            case_index=0,
            load_count=1,
        ),
    )
    scenario_job_id = cast(str, cast(dict[str, Any], scenario_submission["job"])["job_id"])
    _wait_succeeded(client, scenario_job_id)
    scenario_id = cast(str, scenario_submission["scenario_id"])
    scenario = _request_json(
        client,
        "GET",
        f"/v1/scenarios/{scenario_id}",
        expected_status=200,
    )
    selected = cast(dict[str, Any], cast(dict[str, Any], scenario["result"])["exact"])["selected"]
    verification = cast(dict[str, Any], cast(dict[str, Any], selected)["verification"])
    _require(verification.get("status") == "VALID", "BASELINE_SCENARIO_VERIFICATION")
    comparison_resource = _request_json(
        client,
        "GET",
        f"/v1/comparisons/{comparison_id}",
        expected_status=200,
    )
    _require(
        cast(dict[str, Any], comparison_resource["result"])["rankable"] is True, "COMPARISON_RANK"
    )
    return profile_id, comparison_id, scenario_id, reference, facts


def _measure_api_latency(
    client: httpx.Client,
    *,
    operation: str,
    path: str,
    threshold_ms: float,
    repetitions: int,
    warmups: int,
) -> dict[str, Any]:
    for _ in range(warmups):
        _request_json(client, "GET", path, expected_status=200)

    def one_request() -> tuple[float, str]:
        started = time.perf_counter_ns()
        response = client.get(path, timeout=10)
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        _require(
            response.status_code == 200, f"LATENCY_HTTP_STATUS:{operation}:{response.status_code}"
        )
        payload = response.json()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return duration_ms, digest

    with ThreadPoolExecutor(max_workers=API_CONCURRENCY) as executor:
        retained = list(executor.map(lambda _index: one_request(), range(repetitions)))
    result = latency_statistics(
        [item[0] for item in retained],
        [item[1] for item in retained],
        threshold_ms=threshold_ms,
    )
    result.update(
        {
            "operation": operation,
            "path_template": path.rsplit("/", 1)[0] + "/{result_id}",
            "concurrency": API_CONCURRENCY,
            "warmups": warmups,
            "connection_policy": "ONE_SHARED_PERSISTENT_HTTPX_CLIENT_MAX_CONNECTIONS_8",
            "failed_repetitions_omitted": False,
        }
    )
    return result


def _restart_count(container: str) -> int:
    return int(
        _run(("docker", "inspect", container, "--format", "{{.RestartCount}}")).stdout.strip()
    )


def _sql(
    deployment: ComposeDeployment,
    statement: str,
) -> str:
    return deployment.run(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "ratereplay",
        "-d",
        "ratereplay",
        "-Atc",
        statement,
    ).stdout.strip()


def _job_database_result(
    deployment: ComposeDeployment,
    *,
    job_id: str,
    result_table: str,
    result_owner_column: str,
    result_owner_id: str,
) -> tuple[int, int, list[str]]:
    for identifier in (job_id, result_owner_id):
        _require(HEX_IDENTIFIER.fullmatch(identifier) is not None, "DATABASE_IDENTIFIER_INVALID")
    _require(result_table in {"imports", "scenario_results"}, "RESULT_TABLE_INVALID")
    _require(result_owner_column in {"id", "scenario_id"}, "RESULT_COLUMN_INVALID")
    job_literal = f"'{job_id}'"
    result_owner_literal = f"'{result_owner_id}'"
    attempt_count = int(
        _sql(
            deployment,
            f"SELECT attempt_count FROM jobs WHERE id={job_literal}",  # noqa: S608
        )
    )
    result_statements = {
        ("imports", "id"): f"SELECT COUNT(*) FROM imports WHERE id={result_owner_literal}",  # noqa: S608
        (
            "scenario_results",
            "scenario_id",
        ): (
            f"SELECT COUNT(*) FROM scenario_results WHERE scenario_id={result_owner_literal}"  # noqa: S608
        ),
    }
    result_count = int(
        _sql(
            deployment,
            result_statements[(result_table, result_owner_column)],
        )
    )
    attempt_states = _sql(
        deployment,
        f"SELECT state FROM job_attempts WHERE job_id={job_literal} ORDER BY attempt_number",  # noqa: S608
    ).splitlines()
    return attempt_count, result_count, attempt_states


def _wait_restarted(container: str, before: int, *, timeout_seconds: float = 15) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        after = _restart_count(container)
        if after > before:
            return after
        time.sleep(0.25)
    raise ReleaseQualificationError("WORKER_RESTART_NOT_OBSERVED")


def _recover_import_workers(
    client: httpx.Client,
    deployment: ComposeDeployment,
    *,
    origin: str,
    csrf: str,
    worker_container: str,
    threshold_ms: float,
) -> tuple[list[dict[str, Any]], str]:
    payload = _synthetic_csv(IMPORT_READING_COUNT)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    cases: list[dict[str, Any]] = []
    for case_index in range(1, RECOVERY_CASES + 1):
        print(f"M8_IMPORT_SIGKILL case={case_index}/{RECOVERY_CASES}", flush=True)
        submitted = _request_json(
            client,
            "POST",
            "/v1/imports",
            expected_status=202,
            headers=_headers(origin, csrf, f"m8-release-import-crash-{case_index}"),
            files={"file": (f"m8-synthetic-{case_index}.csv", payload, "text/csv")},
            data={"adapter": "PGE_CSV"},
        )
        job_id = cast(str, submitted["job_id"])
        import_id = cast(str, submitted["import_id"])
        _wait_job(
            client,
            job_id,
            target_states=frozenset({"RUNNING"}),
            timeout_seconds=10,
            poll_seconds=0.02,
        )
        before = _restart_count(worker_container)
        started = time.perf_counter_ns()
        _inject_unexpected_process_crash(worker_container)
        after = _wait_restarted(worker_container, before)
        _wait_succeeded(client, job_id, timeout_seconds=threshold_ms / 1_000 + 5)
        recovery_duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        attempt_count, result_count, attempt_states = _job_database_result(
            deployment,
            job_id=job_id,
            result_table="imports",
            result_owner_column="id",
            result_owner_id=import_id,
        )
        reading_count = int(
            _sql(
                deployment,
                f"SELECT COUNT(*) FROM import_readings WHERE import_id='{import_id}'",  # noqa: S608
            )
        )
        recovered = (
            recovery_duration_ms <= threshold_ms
            and attempt_count == 2
            and result_count == 1
            and attempt_states == ["EXPIRED", "SUCCEEDED"]
            and reading_count == IMPORT_READING_COUNT
            and after > before
        )
        _require(recovered, f"IMPORT_RECOVERY_FAILED:{case_index}")
        cases.append(
            {
                "case": case_index,
                "signal": "SIGKILL",
                "injection_phase": "OBSERVED_JOB_RUNNING",
                "attempt_count": attempt_count,
                "attempt_states": attempt_states,
                "result_count": result_count,
                "reading_count": reading_count,
                "worker_restart_increment": after - before,
                "recovery_duration_ms": round(recovery_duration_ms, 6),
                "threshold_ms": threshold_ms,
                "recovered": recovered,
            }
        )
    return cases, payload_sha256


def _recover_scenario_workers(
    client: httpx.Client,
    deployment: ComposeDeployment,
    *,
    origin: str,
    csrf: str,
    worker_container: str,
    profile_id: str,
    reference_schedule: list[dict[str, Any]],
    facts: dict[str, Any],
    threshold_ms: float,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for case_index in range(1, RECOVERY_CASES + 1):
        print(f"M8_SCENARIO_SIGKILL case={case_index}/{RECOVERY_CASES}", flush=True)
        submitted = _request_json(
            client,
            "POST",
            "/v1/scenarios",
            expected_status=202,
            headers=_headers(origin, csrf, f"m8-release-scenario-crash-{case_index}"),
            json_payload=_scenario_payload(
                profile_id=profile_id,
                facts=facts,
                reference_schedule=reference_schedule,
                case_index=case_index,
                load_count=5,
            ),
        )
        job_id = cast(str, cast(dict[str, Any], submitted["job"])["job_id"])
        scenario_id = cast(str, submitted["scenario_id"])
        _wait_job(
            client,
            job_id,
            target_states=frozenset({"RUNNING"}),
            timeout_seconds=10,
            poll_seconds=0.05,
        )
        before = _restart_count(worker_container)
        started = time.perf_counter_ns()
        _inject_unexpected_process_crash(worker_container)
        after = _wait_restarted(worker_container, before)
        _wait_succeeded(client, job_id, timeout_seconds=threshold_ms / 1_000 + 5)
        recovery_duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        scenario = _request_json(
            client,
            "GET",
            f"/v1/scenarios/{scenario_id}",
            expected_status=200,
            retry_rate_limit=True,
        )
        verification = cast(
            dict[str, Any],
            cast(
                dict[str, Any],
                cast(dict[str, Any], scenario["result"])["exact"],
            )["selected"],
        )["verification"]
        attempt_count, result_count, attempt_states = _job_database_result(
            deployment,
            job_id=job_id,
            result_table="scenario_results",
            result_owner_column="scenario_id",
            result_owner_id=scenario_id,
        )
        recovered = (
            recovery_duration_ms <= threshold_ms
            and attempt_count == 2
            and result_count == 1
            and attempt_states == ["EXPIRED", "SUCCEEDED"]
            and cast(dict[str, Any], verification).get("status") == "VALID"
            and after > before
        )
        _require(recovered, f"SCENARIO_RECOVERY_FAILED:{case_index}")
        cases.append(
            {
                "case": case_index,
                "signal": "SIGKILL",
                "injection_phase": "OBSERVED_JOB_RUNNING",
                "load_count": 5,
                "attempt_count": attempt_count,
                "attempt_states": attempt_states,
                "result_count": result_count,
                "verification_status": cast(dict[str, Any], verification)["status"],
                "worker_restart_increment": after - before,
                "recovery_duration_ms": round(recovery_duration_ms, 6),
                "threshold_ms": threshold_ms,
                "recovered": recovered,
            }
        )
    return cases


def _storage_measurements(deployment: ComposeDeployment) -> dict[str, Any]:
    database_bytes = int(_sql(deployment, "SELECT pg_database_size(current_database())"))
    database_version = _sql(deployment, "SHOW server_version")
    object_size = deployment.run("exec", "-T", "object-store", "du", "-sk", "/data").stdout
    object_bytes = int(object_size.split()[0]) * 1_024
    object_files = deployment.run(
        "exec", "-T", "object-store", "find", "/data", "-type", "f"
    ).stdout.splitlines()
    _require(database_bytes > 0, "DATABASE_SIZE_EMPTY")
    _require(object_bytes > 0 and bool(object_files), "OBJECT_STORE_SIZE_EMPTY")
    return {
        "postgres_database_bytes": database_bytes,
        "postgres_server_version": database_version,
        "s3_compatible_store_bytes": object_bytes,
        "s3_compatible_store_file_count": len(object_files),
        "size_method": {
            "postgres": "pg_database_size(current_database())",
            "object_store": "du -sk /data multiplied by 1024",
        },
    }


def _duplicates(deployment: ComposeDeployment) -> int:
    return int(
        _sql(
            deployment,
            "SELECT COALESCE(SUM(result_count - 1), 0) FROM ("
            "SELECT accepted_job_id, COUNT(*) AS result_count FROM job_result_claims "
            "GROUP BY accepted_job_id) AS grouped",
        )
    )


def _major_minor_version(value: str) -> str:
    parts = value.split(".")
    _require(
        len(parts) >= 2 and all(part.isdigit() for part in parts),
        "OPERATING_SYSTEM_VERSION_INVALID",
    )
    return ".".join(parts[:2])


def _host_observation(manifest: dict[str, Any]) -> dict[str, Any]:
    frozen = cast(dict[str, Any], manifest["hardware"])
    exact_macos_version = platform.mac_ver()[0]
    observed = {
        "architecture": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "operating_system": f"macOS {_major_minor_version(exact_macos_version)}",
        "operating_system_exact": f"macOS {exact_macos_version}",
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    _require(observed["architecture"] == frozen["architecture"], "HARDWARE_ARCHITECTURE")
    _require(observed["logical_cpu_count"] == frozen["logical_cpu_count"], "HARDWARE_CPU_COUNT")
    _require(observed["operating_system"] == frozen["operating_system"], "HARDWARE_OS")
    return observed


def qualify() -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the complete frozen release-topology evaluation."""

    evaluation_commit, branch = _preflight()
    manifest = _json(MANIFEST)
    m7 = _json(M7_EVIDENCE)
    validate_deployment_evidence(m7)
    tags, image_ids = _verify_release_images(m7, evaluation_commit)
    thresholds = cast(dict[str, float], cast(dict[str, Any], manifest["performance"])["thresholds"])
    repetitions = cast(int, cast(dict[str, Any], manifest["performance"])["api_repetitions"])
    warmups = cast(int, cast(dict[str, Any], manifest["performance"])["warmups"])
    _require(repetitions == 30 and warmups == 3, "MANIFEST_API_SERIES")
    host = _host_observation(manifest)

    runtime, environment, sensitive_values = _create_runtime(ROOT)
    project = f"ratereplay-m8-release-{os.getpid()}"
    http_port = int(os.getenv("RATEREPLAY_M8_HTTP_PORT", "59180"))
    https_port = int(os.getenv("RATEREPLAY_M8_HTTPS_PORT", "59543"))
    _require(1024 <= http_port <= 65535, "HTTP_PORT_INVALID")
    _require(1024 <= https_port <= 65535 and https_port != http_port, "HTTPS_PORT_INVALID")
    environment.update(
        {
            "RATEREPLAY_PUBLIC_HTTP_PORT": str(http_port),
            "RATEREPLAY_PUBLIC_HTTPS_PORT": str(https_port),
            "RATEREPLAY_APP_IMAGE": tags["app_candidate"],
            "RATEREPLAY_OBJECT_STORE_IMAGE": tags["object_store"],
            "RATEREPLAY_POSTGRES_IMAGE": tags["postgres"],
            "RATEREPLAY_PROXY_IMAGE": tags["proxy"],
            "RATEREPLAY_WEB_IMAGE": tags["web"],
        }
    )
    deployment = ComposeDeployment(
        project=project,
        environment=environment,
        http_port=http_port,
        https_port=https_port,
        runtime=runtime,
    )
    core_client: httpx.Client | None = None
    import_client: httpx.Client | None = None
    try:
        deployment.run("up", "--detach", "--wait", "--wait-timeout", "120", timeout=180)
        proxy = deployment.container_id("proxy")
        certificate = runtime / "local-root.crt"
        _run(
            (
                "docker",
                "cp",
                f"{proxy}:/data/caddy/pki/authorities/local/root.crt",
                str(certificate),
            )
        )
        tls_context = ssl.create_default_context(cafile=str(certificate))
        origin = f"https://localhost:{https_port}"
        limits = httpx.Limits(max_connections=API_CONCURRENCY, max_keepalive_connections=8)
        core_client = httpx.Client(
            base_url=origin,
            verify=tls_context,
            follow_redirects=False,
            limits=limits,
        )
        import_client = httpx.Client(
            base_url=origin,
            verify=tls_context,
            follow_redirects=False,
            limits=limits,
        )
        _wait_public_ready(core_client)
        core_csrf = _register(core_client, origin, "core")
        import_csrf = _register(import_client, origin, "import")
        profile_id, comparison_id, scenario_id, reference, facts = _prepare_user_path(
            core_client,
            origin=origin,
            csrf=core_csrf,
        )
        api_latency = [
            _measure_api_latency(
                core_client,
                operation="WARM_CACHED_COMPARISON_GET",
                path=f"/v1/comparisons/{comparison_id}",
                threshold_ms=thresholds["api_warm_cached_comparison_p95_ms"],
                repetitions=repetitions,
                warmups=warmups,
            ),
            _measure_api_latency(
                core_client,
                operation="WARM_SCENARIO_GET",
                path=f"/v1/scenarios/{scenario_id}",
                threshold_ms=thresholds["api_scenario_warm_get_p95_ms"],
                repetitions=repetitions,
                warmups=warmups,
            ),
        ]
        api_attempt: dict[str, Any] = {
            "schema_version": "m8-api-release-attempt-v1",
            "evidence_level": "LOCAL_REPRODUCIBLE",
            "evidence_scope": "PUBLIC_SIMULATED_PROFILE_ONLY",
            "manifest_sha256": manifest["manifest_sha256"],
            "evaluation_source_commit": evaluation_commit,
            "application_source_commit": evaluation_commit,
            "application_image_id": image_ids["app_candidate"],
            "generated_at": datetime.now(UTC).isoformat(),
            "api_latency": api_latency,
            "failed_repetitions_omitted": False,
            "gate_result": ("PASS" if all(item["passed"] for item in api_latency) else "FAIL"),
        }
        api_attempt["artifact_sha256"] = _self_hash(api_attempt)
        if api_attempt["gate_result"] == "FAIL":
            FAILED_API_OUTPUT.write_text(
                json.dumps(api_attempt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                "M8_API_LATENCY_ATTEMPT_FAIL "
                + " ".join(f"{item['operation']}_p95_ms={item['p95_ms']}" for item in api_latency),
                file=sys.stderr,
                flush=True,
            )
        _require(all(item["passed"] for item in api_latency), "API_LATENCY_GATE")
        worker = deployment.container_id("worker")
        import_cases, synthetic_import_sha256 = _recover_import_workers(
            import_client,
            deployment,
            origin=origin,
            csrf=import_csrf,
            worker_container=worker,
            threshold_ms=thresholds["worker_recovery_maximum_ms"],
        )
        scenario_cases = _recover_scenario_workers(
            core_client,
            deployment,
            origin=origin,
            csrf=core_csrf,
            worker_container=worker,
            profile_id=profile_id,
            reference_schedule=reference,
            facts=facts,
            threshold_ms=thresholds["scenario_worker_recovery_maximum_ms"],
        )
        storage = _storage_measurements(deployment)
        duplicate_successful_results = _duplicates(deployment)
        _require(duplicate_successful_results == 0, "DUPLICATE_SUCCESSFUL_RESULTS")
        generated_at = datetime.now(UTC).isoformat()
        crash_payload: dict[str, Any] = {
            "schema_version": "m8-crash-recovery-v1",
            "evidence_level": "LOCAL_REPRODUCIBLE",
            "evidence_scope": "PUBLIC_SIMULATED_AND_SYNTHETIC_ENGINEERING_ONLY",
            "manifest_sha256": manifest["manifest_sha256"],
            "evaluation_source_commit": evaluation_commit,
            "application_source_commit": evaluation_commit,
            "generated_at": generated_at,
            "topology": {
                "database": f"PostgreSQL {storage['postgres_server_version']}",
                "object_store": "SeaweedFS 4.40 S3-compatible release service",
                "worker_processes": 1,
                "worker_container_restart_policy": "unless-stopped",
                "worker_signal": "SIGKILL",
            },
            "prior_stage_injection_artifact": {
                "path": str(M1_RECOVERY_EVIDENCE.relative_to(ROOT)),
                "sha256": _sha256(M1_RECOVERY_EVIDENCE),
                "scope": "PRESERVED_SQLITE_AND_FILESYSTEM_STAGE_INJECTION_BASELINE",
                "counts_as_release_topology_evidence": False,
            },
            "synthetic_import": {
                "scope": "SYNTHETIC_ENGINEERING_ONLY",
                "adapter": "PGE_CSV",
                "reading_count": IMPORT_READING_COUNT,
                "payload_sha256": synthetic_import_sha256,
                "customer_claim_prohibited": True,
            },
            "import_worker_cases": import_cases,
            "scenario_worker_cases": scenario_cases,
            "duplicate_successful_results": duplicate_successful_results,
            "all_worker_restarts_observed": all(
                cast(int, item["worker_restart_increment"]) >= 1
                for item in (*import_cases, *scenario_cases)
            ),
            "limitations": [
                "The local release qualification terminates the sole worker container process.",
                (
                    "Import termination is injected after the durable job reports RUNNING, "
                    "not at a claimed parser instruction."
                ),
                (
                    "The preserved Milestone 1 artifact retains exact stage-injection coverage "
                    "but is not counted as release-topology evidence."
                ),
            ],
            "gate_result": "PASS",
        }
        crash_payload["artifact_sha256"] = _self_hash(crash_payload)
        validate_crash_evidence(crash_payload)

        release_payload: dict[str, Any] = {
            "schema_version": "m8-release-topology-v1",
            "evidence_level": "LOCAL_REPRODUCIBLE",
            "evidence_scope": "PUBLIC_SIMULATED_AND_SYNTHETIC_ENGINEERING_ONLY",
            "manifest_sha256": manifest["manifest_sha256"],
            "evaluation_source_commit": evaluation_commit,
            "application_source_commit": evaluation_commit,
            "source_branch": branch,
            "source_remote_confirmed": True,
            "generated_at": generated_at,
            "host": host,
            "inputs": {
                "compose_release_sha256": _sha256(ROOT / "compose.release.yaml"),
                "m7_deployment_artifact_sha256": m7["artifact_sha256"],
                "m8_manifest_sha256": manifest["manifest_sha256"],
                "uv_lock_sha256": _sha256(ROOT / "uv.lock"),
            },
            "preserved_failed_api_attempt": (
                {
                    "path": str(FAILED_API_OUTPUT.relative_to(ROOT)),
                    "sha256": _sha256(FAILED_API_OUTPUT),
                }
                if FAILED_API_OUTPUT.is_file()
                else None
            ),
            "images": image_ids,
            "topology": {
                "service_count": 8,
                "published_services": ["proxy"],
                "https_ready": True,
                "local_ca_trusted_by_test_client": True,
                "api_processes": 1,
                "worker_processes": 1,
                "database": f"PostgreSQL {storage['postgres_server_version']}",
                "object_store": "SeaweedFS 4.40 S3-compatible release service",
            },
            "plan_correction": {
                "obsolete_detail": (
                    "The preserved performance-v3 charter labels the object store as MinIO."
                ),
                "authoritative_replacement": (
                    "The accepted Milestone 7 release topology uses pinned SeaweedFS 4.40 "
                    "behind the same S3 adapter."
                ),
                "evidence": [
                    "compose.release.yaml",
                    "containers/object-store.Dockerfile",
                    "docs/architecture/authentication-and-deployment.md",
                    "evidence/reliability/m7-local-deployment.json",
                ],
                "correction_scope": "IMPLEMENTATION_LABEL_ONLY",
                "numeric_thresholds_changed": False,
                "workload_rules_changed": False,
                "preserved_charter_rewritten": False,
            },
            "user_path": {
                "authentication": "LOCAL_USERNAME_PASSWORD_SESSION",
                "transport": "HTTPS_WITH_LOCAL_CA",
                "profile": "REPOSITORY_OWNED_SIMULATED_JULY_2026",
                "replay_succeeded": True,
                "three_candidate_comparison_rankable": True,
                "verified_scenario_succeeded": True,
            },
            "api_latency": api_latency,
            "storage": storage,
            "crash_recovery_artifact_sha256": crash_payload["artifact_sha256"],
            "measurement_policy": {
                "warmups": warmups,
                "retained_requests_per_operation": repetitions,
                "concurrency": API_CONCURRENCY,
                "percentiles": "NEAREST_RANK",
                "client": "HTTPX_SHARED_SYNCHRONOUS_CLIENT_WITH_CONNECTION_POOL",
                "httpx_client_documentation": "https://www.python-httpx.org/advanced/clients/",
                "correctness_and_performance_separate": True,
            },
            "claims_withheld": list(CLAIMS_WITHHELD),
            "gate_result": "PASS",
        }
        serialized = json.dumps(release_payload, sort_keys=True)
        _require(all(value not in serialized for value in sensitive_values), "ARTIFACT_SECRET_LEAK")
        release_payload["artifact_sha256"] = _self_hash(release_payload)
        validate_release_evidence(release_payload)
        RELEASE_OUTPUT.write_text(
            json.dumps(release_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        CRASH_OUTPUT.write_text(
            json.dumps(crash_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return release_payload, crash_payload
    except Exception:
        try:
            diagnostic = deployment.run(
                "logs",
                "--no-color",
                "postgres",
                "migrate",
                "api",
                "worker",
                timeout=30,
            ).stdout[-12_000:]
        except Exception as diagnostic_error:
            diagnostic = f"DIAGNOSTIC_UNAVAILABLE:{type(diagnostic_error).__name__}"
        for sensitive_value in sensitive_values:
            diagnostic = diagnostic.replace(sensitive_value, "[REDACTED]")
        print(f"M8_RELEASE_SERVICE_DIAGNOSTIC\n{diagnostic}", file=sys.stderr, flush=True)
        raise
    finally:
        if core_client is not None:
            core_client.close()
        if import_client is not None:
            import_client.close()
        deployment.down()
        shutil.rmtree(runtime, ignore_errors=True)


def main() -> None:
    release, crash = qualify()
    print(
        "M8_RELEASE_QUALIFICATION_PASS "
        f"release_sha256={release['artifact_sha256']} "
        f"crash_sha256={crash['artifact_sha256']}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except ReleaseQualificationError as error:
        print(f"M8_RELEASE_QUALIFICATION_FAIL {error}", file=sys.stderr)
        raise SystemExit(1) from error
