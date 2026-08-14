"""Locked PG&E Green Button CSV adapter."""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Final
from zoneinfo import ZoneInfo

from ratereplay_domain.energy import EnergyAdmissionError, exact_watt_hours

ADAPTER_FINGERPRINT: Final = "pge-green-button-csv-v1"
EXPECTED_HEADER: Final = (
    "TYPE",
    "DATE",
    "START TIME",
    "END TIME",
    "USAGE",
    "UNITS",
    "COST",
    "NOTES",
)
EXPECTED_PROLOGUE_KEYS: Final = ("Name", "Address", "Account Number", "Service")
MAX_CSV_BYTES: Final = 20 * 1024 * 1024
MAX_CSV_ROWS: Final = 100_000
PACIFIC: Final = ZoneInfo("America/Los_Angeles")


class PgeCsvError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CsvFinding:
    code: str
    severity: str
    field_path: str


@dataclass(frozen=True, slots=True)
class CsvReading:
    start_utc_seconds: int
    duration_seconds: int
    energy_wh: int


@dataclass(frozen=True, slots=True)
class PgeCsvDocument:
    source_hash: str
    adapter_fingerprint: str
    timezone: str
    interval_seconds: int
    readings: tuple[CsvReading, ...]
    findings: tuple[CsvFinding, ...]


def _local_candidates(local: datetime) -> tuple[datetime, ...]:
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=PACIFIC, fold=fold)
        utc_value = aware.astimezone(UTC)
        if utc_value.astimezone(PACIFIC).replace(tzinfo=None) == local:
            candidates.append(utc_value)
    unique = {candidate: candidate for candidate in candidates}
    return tuple(sorted(unique.values()))


def _parse_local(date_text: str, time_text: str) -> datetime:
    try:
        return datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
    except ValueError as error:
        raise PgeCsvError("INVALID_LOCAL_TIMESTAMP", "Invalid provider timestamp") from error


def _parse_rows(payload: bytes) -> list[list[str]]:
    if not payload:
        raise PgeCsvError("EMPTY_FILE", "CSV payload is empty")
    if len(payload) > MAX_CSV_BYTES:
        raise PgeCsvError("OVERSIZED_FILE", "CSV payload exceeds the locked size limit")
    if not payload.startswith(b"\xef\xbb\xbf"):
        raise PgeCsvError("UNKNOWN_CSV_FINGERPRINT", "Expected provider UTF-8 byte-order mark")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PgeCsvError("INVALID_CSV_ENCODING", "CSV is not valid UTF-8") from error
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        rows = list(reader)
    except csv.Error as error:
        raise PgeCsvError("MALFORMED_CSV", "CSV structure is malformed") from error
    if len(rows) > MAX_CSV_ROWS + 6:
        raise PgeCsvError("TOO_MANY_ROWS", "CSV exceeds the locked row limit")
    return rows


def parse_pge_csv(payload: bytes) -> PgeCsvDocument:
    """Parse the provider fingerprint into exact UTC interval energy."""

    rows = _parse_rows(payload)
    if len(rows) < 7:
        raise PgeCsvError("UNKNOWN_CSV_FINGERPRINT", "Provider prologue or header is missing")
    for index, expected_key in enumerate(EXPECTED_PROLOGUE_KEYS):
        row = rows[index]
        if len(row) != 2 or row[0] != expected_key or not row[1].strip():
            raise PgeCsvError("UNKNOWN_CSV_FINGERPRINT", "Provider prologue does not match")
    if rows[4] != [] or tuple(rows[5]) != EXPECTED_HEADER:
        raise PgeCsvError("UNKNOWN_CSV_FINGERPRINT", "Provider header does not match")

    readings: list[CsvReading] = []
    ambiguous_counts: Counter[datetime] = Counter()
    findings: list[CsvFinding] = []
    interval_seconds: int | None = None
    for row in rows[6:]:
        if len(row) != len(EXPECTED_HEADER):
            raise PgeCsvError("MALFORMED_CSV", "CSV row has an unexpected field count")
        usage_type, date_text, start_text, end_text, usage, unit, _, _ = row
        if usage_type != "Electric usage":
            raise PgeCsvError("UNSUPPORTED_READING_SEMANTICS", "Row is not imported electricity")
        if unit != "kWh":
            raise PgeCsvError("UNKNOWN_ENERGY_UNIT", "Row unit is not kilowatt-hours")
        local_start = _parse_local(date_text, start_text)
        local_end_label = _parse_local(date_text, end_text)
        if local_end_label < local_start:
            local_end_label += timedelta(days=1)
        nominal_duration = local_end_label + timedelta(minutes=1) - local_start
        duration = int(nominal_duration.total_seconds())
        if duration not in {900, 3600}:
            raise PgeCsvError("UNSUPPORTED_INTERVAL_DURATION", "Row duration is not admitted")
        if interval_seconds is None:
            interval_seconds = duration
        elif interval_seconds != duration:
            raise PgeCsvError("MIXED_INTERVAL_DURATIONS", "CSV mixes interval resolutions")

        candidates = _local_candidates(local_start)
        if not candidates:
            raise PgeCsvError("NONEXISTENT_LOCAL_TIMESTAMP", "Timestamp falls in a DST gap")
        occurrence = ambiguous_counts[local_start]
        if len(candidates) == 2:
            if occurrence >= 2:
                raise PgeCsvError("AMBIGUOUS_LOCAL_TIMESTAMP", "DST clock occurs too many times")
            ambiguous_counts[local_start] += 1
            start_utc = candidates[occurrence]
        else:
            start_utc = candidates[0]
        try:
            energy_wh = exact_watt_hours(usage, source_unit="kWh")
        except EnergyAdmissionError as error:
            raise PgeCsvError(error.code, str(error)) from error
        readings.append(CsvReading(int(start_utc.timestamp()), duration, energy_wh))

    if interval_seconds is None or not readings:
        raise PgeCsvError("EMPTY_FILE", "CSV contains no interval rows")
    unmatched = [local for local, count in ambiguous_counts.items() if count != 2]
    if unmatched:
        raise PgeCsvError("AMBIGUOUS_LOCAL_TIMESTAMP", "DST overlap is missing its paired clock")
    keys = [(reading.start_utc_seconds, reading.duration_seconds) for reading in readings]
    if keys != sorted(keys):
        raise PgeCsvError("NON_MONOTONIC_INTERVALS", "Provider rows are not UTC-monotonic")
    if len(keys) != len(set(keys)):
        raise PgeCsvError("DUPLICATE_INTERVALS", "Provider rows contain a duplicate interval")
    for prior, current in pairwise(readings):
        prior_end = prior.start_utc_seconds + prior.duration_seconds
        if current.start_utc_seconds < prior_end:
            raise PgeCsvError("OVERLAPPING_INTERVALS", "Provider intervals overlap")
        if current.start_utc_seconds > prior_end:
            findings.append(
                CsvFinding("INTERVAL_GAP", "WARNING", f"readings[{current.start_utc_seconds}]")
            )

    return PgeCsvDocument(
        source_hash=hashlib.sha256(payload).hexdigest(),
        adapter_fingerprint=ADAPTER_FINGERPRINT,
        timezone="America/Los_Angeles",
        interval_seconds=interval_seconds,
        readings=tuple(readings),
        findings=tuple(sorted(set(findings), key=lambda finding: finding.field_path)),
    )
