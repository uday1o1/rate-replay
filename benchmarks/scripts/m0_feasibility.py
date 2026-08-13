#!/usr/bin/env python3
"""Measure Milestone 0 feasibility paths on the named local machine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import statistics
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import httpx
from argon2 import PasswordHasher
from ratereplay_api.main import app
from ratereplay_ingestion.espi_spike import parse_espi
from ratereplay_tariffs.ir import e1_july_2026_ir, evaluate_compiled_ir

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evidence/performance/m0-feasibility.json"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _latencies(operation: Callable[[], object], repetitions: int, warmups: int) -> list[float]:
    for _ in range(warmups):
        operation()
    samples = []
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return samples


def _summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)

    def percentile(percent: float) -> float:
        index = max(0, min(len(ordered) - 1, int(percent * len(ordered) + 0.999999) - 1))
        return round(ordered[index], 6)

    return {
        "maximum_ms": round(max(samples), 6),
        "median_ms": round(statistics.median(samples), 6),
        "minimum_ms": round(min(samples), 6),
        "p95_ms": percentile(0.95),
        "repetitions": len(samples),
    }


async def _api_latencies(repetitions: int, warmups: int) -> list[float]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://benchmark") as client:
        for _ in range(warmups):
            response = await client.get("/v1/meta")
            response.raise_for_status()
        samples = []
        for _ in range(repetitions):
            started = time.perf_counter_ns()
            response = await client.get("/v1/meta")
            response.raise_for_status()
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        return samples


def main() -> None:
    fixture = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"
    schema = ROOT / "third_party/espi-schema/espi-4.0.xsd"
    payload = fixture.read_bytes()

    def parser_operation() -> object:
        return parse_espi(payload, schema_path=schema)

    parser_samples = _latencies(parser_operation, repetitions=20, warmups=3)
    tracemalloc.start()
    parser_operation()
    _, parser_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    ir = e1_july_2026_ir(baseline_wh=201_500)
    replay_samples = _latencies(
        lambda: evaluate_compiled_ir(ir, energy_wh=310_000, billing_days=31),
        repetitions=100,
        warmups=10,
    )
    api_samples = asyncio.run(_api_latencies(repetitions=100, warmups=10))

    hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)
    benchmark_secret = "benchmark-only-not-a-credential"  # noqa: S105
    hash_samples = []
    verify_samples = []
    for _ in range(5):
        started = time.perf_counter_ns()
        verifier = hasher.hash(benchmark_secret)
        hash_samples.append((time.perf_counter_ns() - started) / 1_000_000)
        started = time.perf_counter_ns()
        if not hasher.verify(verifier, benchmark_secret):
            raise AssertionError("Argon2 verifier failed")
        verify_samples.append((time.perf_counter_ns() - started) / 1_000_000)

    measurements: dict[str, dict[str, int | float]] = {
        "api_metadata": _summary(api_samples),
        "argon2id_hash": _summary(hash_samples),
        "argon2id_verify": _summary(verify_samples),
        "e1_reference_replay": _summary(replay_samples),
        "espi_parse_and_schema_validate": {
            **_summary(parser_samples),
            "tracemalloc_peak_bytes": parser_peak,
        },
    }
    thresholds = {
        "api_metadata_p95_ms": 20,
        "argon2id_hash_maximum_ms": 1500,
        "argon2id_verify_maximum_ms": 1500,
        "e1_reference_replay_p95_ms": 10,
        "espi_parse_p95_ms": 250,
        "espi_tracemalloc_peak_bytes": 33_554_432,
    }
    passed = all(
        [
            measurements["api_metadata"]["p95_ms"] <= thresholds["api_metadata_p95_ms"],
            measurements["argon2id_hash"]["maximum_ms"] <= thresholds["argon2id_hash_maximum_ms"],
            measurements["argon2id_verify"]["maximum_ms"]
            <= thresholds["argon2id_verify_maximum_ms"],
            measurements["e1_reference_replay"]["p95_ms"]
            <= thresholds["e1_reference_replay_p95_ms"],
            measurements["espi_parse_and_schema_validate"]["p95_ms"]
            <= thresholds["espi_parse_p95_ms"],
            measurements["espi_parse_and_schema_validate"]["tracemalloc_peak_bytes"]
            <= thresholds["espi_tracemalloc_peak_bytes"],
        ]
    )
    result: dict[str, object] = {
        "benchmark_version": "m0-feasibility-v1",
        "cache_state": "warm process after declared warmups",
        "hardware": {
            "architecture": platform.machine(),
            "logical_cpu_count": 10,
            "machine": "Apple M5 developer laptop",
            "memory_gib": 24,
            "operating_system": platform.platform(),
        },
        "measurements": measurements,
        "passed": passed,
        "process_topology": "single Python 3.12 process with in-process ASGI transport",
        "thresholds": thresholds,
        "workloads": {
            "canonical_profile_golden_sha256": (
                "60f888697a7f2df0457f9555006277e56849fea88eda2a8dcf4554ba3d4011c3"
            ),
            "e1_golden_sha256": _sha256(
                (ROOT / "tariffs/golden/e1-july-2026-complete-bill.json").read_bytes()
            ),
            "espi_fixture_sha256": _sha256(payload),
            "espi_schema_sha256": _sha256(schema.read_bytes()),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not passed:
        raise SystemExit("M0_FEASIBILITY_THRESHOLD_FAILED")


if __name__ == "__main__":
    main()
