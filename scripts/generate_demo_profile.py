#!/usr/bin/env python3
"""Derive the frozen July 2026 simulated profile from a locked NREL aggregate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_SHA256 = "db3eb84a48798d19423617f73300367fcaf53a8946b9da40032cf3c6b1246dd3"
TARGET_TOTAL_WH = 750_000
SOURCE_START = datetime(2018, 7, 1, 0, 15)
SOURCE_END = datetime(2018, 8, 1, 0, 0)
TARGET_TIMEZONE = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True, slots=True)
class WeightedInterval:
    source_end: datetime
    weight: Decimal


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_shape(source_path: Path) -> list[WeightedInterval]:
    if _sha256(source_path) != SOURCE_SHA256:
        raise ValueError("NREL_SOURCE_HASH_MISMATCH")
    selected: list[WeightedInterval] = []
    with source_path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            source_end = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            if SOURCE_START <= source_end <= SOURCE_END:
                weight = Decimal(row["out.electricity.total.energy_consumption"])
                if weight < 0:
                    raise ValueError("NREL_NEGATIVE_AGGREGATE")
                selected.append(WeightedInterval(source_end, weight))
    if len(selected) != 31 * 96:
        raise ValueError(f"NREL_INTERVAL_COUNT:{len(selected)}")
    return selected


def _allocate_integer_wh(shape: list[WeightedInterval]) -> list[int]:
    total_weight = sum((item.weight for item in shape), start=Decimal(0))
    if total_weight <= 0:
        raise ValueError("NREL_EMPTY_SHAPE")
    with localcontext() as context:
        context.prec = 80
        exact = [item.weight * TARGET_TOTAL_WH / total_weight for item in shape]
    floors = [int(value) for value in exact]
    remainder_count = TARGET_TOTAL_WH - sum(floors)
    ranked = sorted(
        range(len(exact)),
        key=lambda index: (exact[index] - floors[index], -index),
        reverse=True,
    )
    for index in ranked[:remainder_count]:
        floors[index] += 1
    if sum(floors) != TARGET_TOTAL_WH:
        raise AssertionError("Integer allocation failed energy conservation")
    return floors


def generate(source_path: Path, output_path: Path, lock_path: Path) -> None:
    shape = _read_shape(source_path)
    energy_wh = _allocate_integer_wh(shape)
    readings = []
    for item, energy in zip(shape, energy_wh, strict=True):
        target_end_local = item.source_end.replace(year=2026, tzinfo=TARGET_TIMEZONE)
        start_utc = (target_end_local - timedelta(minutes=15)).astimezone(UTC)
        readings.append(
            {
                "duration_seconds": 900,
                "energy_wh": energy,
                "start_utc": start_utc.isoformat().replace("+00:00", "Z"),
            }
        )
    payload = {
        "interval_resolution_seconds": 900,
        "label": "SIMULATED NREL-derived California household",
        "profile_schema_version": "simulated-profile-v1",
        "readings": readings,
        "service_window_local": ["2026-07-01", "2026-08-01"],
        "source_timezone_interpretation": (
            "15-minute interval-ending local clock remapped to America/Los_Angeles"
        ),
        "tariff_timezone": "America/Los_Angeles",
        "total_energy_wh": TARGET_TOTAL_WH,
        "transformation": (
            "July 2018 aggregate load shape remapped by month, day, and clock time, "
            "then normalized to 750000 Wh with largest-remainder integer allocation"
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    output_path.write_bytes(output_bytes)
    lock = {
        "artifact_path": output_path.as_posix(),
        "artifact_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "generator": "scripts/generate_demo_profile.py",
        "generator_contract_version": "v1",
        "source_sha256": SOURCE_SHA256,
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("data/demo/july-2026-simulated-profile.json")
    )
    parser.add_argument("--lock", type=Path, default=Path("data/demo/profile.lock.json"))
    arguments = parser.parse_args()
    generate(arguments.source, arguments.output, arguments.lock)


if __name__ == "__main__":
    main()
