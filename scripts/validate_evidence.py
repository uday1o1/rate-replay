#!/usr/bin/env python3
"""Validate locked repository evidence without network access."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ratereplay_domain.energy import exact_watt_hours

ROOT = Path(__file__).resolve().parents[1]


class EvidenceValidationError(RuntimeError):
    """A locked evidence invariant does not hold."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


def _json(path: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ROOT / path).read_text(encoding="utf-8")))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_external_sources() -> None:
    source_lock = _json("data/sources.lock.json")
    for source in source_lock["sources"]:
        artifact_path = source["artifact_path"]
        if artifact_path is not None:
            _require(
                _sha256(ROOT / artifact_path) == source["sha256"],
                f"SOURCE_HASH_MISMATCH:{source['source_id']}",
            )
    contract = _json("data/contracts/espi-admission-v1.json")
    _require(
        _sha256(ROOT / contract["schema_artifact"]) == contract["schema_sha256"],
        "ESPI_SCHEMA_HASH_MISMATCH",
    )
    _require(
        contract["fatal_nonintegral_code"] == "NON_INTEGRAL_WATT_HOUR",
        "ENERGY_ERROR_CODE_DRIFT",
    )


def _validate_csv() -> None:
    path = ROOT / "third_party/pge-csv/provider-sample.csv"
    payload = path.read_bytes()
    _require(payload.startswith(b"\xef\xbb\xbfName,SAMPLE"), "CSV_BOM_OR_REDACTION_MISMATCH")
    lines = payload.decode("utf-8-sig").splitlines()
    _require(
        lines[:4]
        == [
            "Name,SAMPLE",
            'Address,"SAMPLE"',
            "Account Number,SAMPLE",
            "Service,SAMPLE",
        ],
        "CSV_PROLOGUE_MISMATCH",
    )
    _require(
        lines[5] == "TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST,NOTES",
        "CSV_HEADER_MISMATCH",
    )
    reader = csv.DictReader(lines[5:])
    count = 0
    for row in reader:
        _require(row["TYPE"] == "Electric usage", "CSV_TYPE_MISMATCH")
        _require(row["UNITS"] == "kWh", "CSV_UNIT_MISMATCH")
        exact_watt_hours(row["USAGE"], source_unit="kWh", power_of_ten_multiplier=0)
        start = datetime.strptime(row["START TIME"], "%H:%M")
        end = datetime.strptime(row["END TIME"], "%H:%M") + timedelta(minutes=1)
        if end <= start:
            end += timedelta(days=1)
        _require(
            end - start in {timedelta(minutes=15), timedelta(hours=1)},
            "CSV_INTERVAL_DURATION_MISMATCH",
        )
        count += 1
    _require(count == 5_664, "CSV_ROW_COUNT_MISMATCH")


def _validate_tariffs() -> None:
    lock = _json("tariffs/sources.lock.json")
    source_ids = {source["source_id"] for source in lock["sources"]}
    _require(
        {"pge-advice-7921-e", "pge-advice-7846-e"} <= source_ids,
        "E1_STABLE_SOURCE_MISSING",
    )
    vectors = lock["tariff_component_vectors"]
    e1 = next(vector for vector in vectors if vector["tariff_id"] == "pge-e1-2026-07")
    _require(
        e1["service_window"] == ["2026-07-01", "2026-08-01"],
        "E1_SERVICE_WINDOW_MISMATCH",
    )
    _require(e1["coverage_count_per_service_instant"] == 1, "E1_VECTOR_COVERAGE_MISMATCH")
    _require(len(e1["components"]) == 2, "E1_COMPONENT_COUNT_MISMATCH")
    golden = _json("tariffs/golden/e1-july-2026-complete-bill.json")
    _require(set(golden["source_ids"]) <= source_ids, "E1_GOLDEN_SOURCE_MISSING")
    matrix = _json("tariffs/admission/candidate-matrix-v1.json")
    _require(len(matrix["tariffs"]) == 5, "CANDIDATE_COUNT_MISMATCH")
    for candidate in matrix["tariffs"]:
        _require(candidate["admission_status"] != "ADMITTED", "PREMATURE_TARIFF_ADMISSION")
    holiday = _json("tariffs/calendars/ca-observed-holidays-2026.json")
    _require(
        holiday["holidays_used_in_july_window"]
        == [{"date": "2026-07-03", "name": "Independence Day observed"}],
        "JULY_HOLIDAY_LOCK_MISMATCH",
    )


def _validate_generated_evidence() -> None:
    profile_lock = _json("data/demo/profile.lock.json")
    _require(
        _sha256(ROOT / profile_lock["artifact_path"]) == profile_lock["artifact_sha256"],
        "DEMO_PROFILE_HASH_MISMATCH",
    )
    profile = _json(profile_lock["artifact_path"])
    _require(len(profile["readings"]) == 31 * 96, "DEMO_PROFILE_INTERVAL_COUNT_MISMATCH")
    _require(
        sum(reading["energy_wh"] for reading in profile["readings"]) == 750_000,
        "DEMO_PROFILE_ENERGY_MISMATCH",
    )
    _require(
        all(isinstance(reading["energy_wh"], int) for reading in profile["readings"]),
        "DEMO_PROFILE_NONINTEGER_ENERGY",
    )
    feasibility = _json("evidence/performance/m0-feasibility.json")
    _require(feasibility["passed"] is True, "M0_FEASIBILITY_FAILED")
    charter = _json("benchmarks/charters/performance-v1.json")
    _require(charter["duplicate_result_threshold"] == 0, "DUPLICATE_THRESHOLD_DRIFT")
    _require(
        charter["thresholds"]["worker_recovery_maximum_ms"] == 30_000,
        "RECOVERY_THRESHOLD_DRIFT",
    )
    allowlist = _json("artifacts/demo/allowlist.v1.json")
    schema = _json("artifacts/demo/manifest.schema.json")
    _require(len(allowlist["logical_artifact_ids"]) == 9, "DEMO_ALLOWLIST_COUNT_MISMATCH")
    _require(bool(allowlist["prohibited_capabilities"]), "DEMO_PROHIBITIONS_MISSING")
    _require(
        schema["properties"]["generation_command"]["const"] == "make demo-artifacts",
        "DEMO_GENERATION_COMMAND_DRIFT",
    )


def _validate_m1_evidence() -> None:
    charter = _json("benchmarks/charters/performance-v1.json")
    recovery = _json("evidence/performance/m1-import-recovery.json")
    _require(recovery["passed"] is True, "M1_RECOVERY_FAILED")
    _require(recovery["charter_version"] == charter["charter_version"], "M1_CHARTER_DRIFT")
    _require(
        recovery["charter_sha256"] == _sha256(ROOT / "benchmarks/charters/performance-v1.json"),
        "M1_CHARTER_HASH_MISMATCH",
    )
    _require(
        recovery["fixture_sha256"]
        == _sha256(ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"),
        "M1_RECOVERY_FIXTURE_HASH_MISMATCH",
    )
    _require(
        recovery["schema_sha256"] == _sha256(ROOT / "third_party/espi-schema/espi-4.0.xsd"),
        "M1_RECOVERY_SCHEMA_HASH_MISMATCH",
    )
    _require(len(recovery["recovery_cases"]) == 10, "M1_RECOVERY_CASE_COUNT_MISMATCH")
    _require(
        {result["case"] for result in recovery["recovery_cases"]}
        == {"before_parse", "during_parse", "before_publish", "after_publish"},
        "M1_RECOVERY_CRASH_COVERAGE_MISMATCH",
    )
    _require(
        all(result["recovered"] is True for result in recovery["recovery_cases"]),
        "M1_RECOVERY_CASE_FAILED",
    )
    _require(recovery["lease_seconds"] == 20, "M1_LEASE_DURATION_DRIFT")
    _require(recovery["poll_seconds"] <= 1, "M1_WORKER_POLL_DRIFT")
    _require(
        recovery["maximum_recovery_upper_bound_ms"]
        <= charter["thresholds"]["worker_recovery_maximum_ms"],
        "M1_RECOVERY_THRESHOLD_FAILED",
    )
    _require(recovery["duplicate_draft_rows"] == 0, "M1_DUPLICATE_DRAFT")
    _require(recovery["duplicate_terminal_results"] == 0, "M1_DUPLICATE_TERMINAL")


def main() -> None:
    _validate_external_sources()
    _validate_csv()
    _validate_tariffs()
    _validate_generated_evidence()
    _validate_m1_evidence()
    print("Repository evidence locks are internally consistent through Milestone 1.")


if __name__ == "__main__":
    main()
