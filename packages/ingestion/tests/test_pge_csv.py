from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_ingestion.pge_csv import PgeCsvError, parse_pge_csv

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "third_party/pge-csv/provider-sample.csv"
HEADER = (
    "\ufeffName,SAMPLE\n"
    "Address,SAMPLE\n"
    "Account Number,SAMPLE\n"
    "Service,SAMPLE\n"
    "\n"
    "TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST,NOTES\n"
)


def _csv(*rows: str) -> bytes:
    return (HEADER + "\n".join(rows) + "\n").encode()


def test_locked_provider_fixture_is_accepted_exactly() -> None:
    document = parse_pge_csv(FIXTURE.read_bytes())
    assert document.adapter_fingerprint == "pge-green-button-csv-v1"
    assert document.timezone == "America/Los_Angeles"
    assert document.interval_seconds == 900
    assert len(document.readings) == 5_664
    assert all(reading.energy_wh >= 0 for reading in document.readings)


def test_csv_parser_consumes_a_bounded_binary_stream() -> None:
    payload = FIXTURE.read_bytes()

    class GuardedStream(BytesIO):
        def readline(self, size: int | None = -1) -> bytes:
            assert size is not None and 0 < size <= 64 * 1024 + 1
            return super().readline(size)

    document = parse_pge_csv(GuardedStream(payload))
    assert document.source_hash == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "EMPTY_FILE"),
        (_csv("Electric usage,2026-01-01,00:00,00:14,0.1,Wh,,"), "UNKNOWN_ENERGY_UNIT"),
        (
            _csv("Electric usage,2026-01-01,00:00,00:14,0.0001,kWh,,"),
            "NON_INTEGRAL_WATT_HOUR",
        ),
        (
            _csv("Electric usage,2026-03-08,02:00,02:14,0.1,kWh,,"),
            "NONEXISTENT_LOCAL_TIMESTAMP",
        ),
        (
            _csv("Electric usage,2026-11-01,01:00,01:14,0.1,kWh,,"),
            "AMBIGUOUS_LOCAL_TIMESTAMP",
        ),
        (
            _csv(
                "Electric usage,2026-01-01,00:15,00:29,0.1,kWh,,",
                "Electric usage,2026-01-01,00:00,00:14,0.1,kWh,,",
            ),
            "NON_MONOTONIC_INTERVALS",
        ),
    ],
)
def test_csv_admission_failures_are_stable(payload: bytes, code: str) -> None:
    with pytest.raises(PgeCsvError) as raised:
        parse_pge_csv(payload)
    assert raised.value.code == code


def test_unknown_header_fingerprint_is_rejected() -> None:
    payload = _csv("Electric usage,2026-01-01,00:00,00:14,0.1,kWh,,").replace(
        b"USAGE", b"ENERGY", 1
    )
    with pytest.raises(PgeCsvError) as raised:
        parse_pge_csv(payload)
    assert raised.value.code == "UNKNOWN_CSV_FINGERPRINT"


def test_fall_back_clocks_map_once_to_each_distinct_utc_instant() -> None:
    rows = []
    for hour in (0, 1, 1, 2):
        for minute in (0, 15, 30, 45):
            rows.append(
                f"Electric usage,2026-11-01,{hour:02d}:{minute:02d},"
                f"{hour:02d}:{minute + 14:02d},0.1,kWh,,"
            )
    document = parse_pge_csv(_csv(*rows))
    assert len(document.readings) == 16
    assert all(
        current.start_utc_seconds - previous.start_utc_seconds == 900
        for previous, current in zip(document.readings, document.readings[1:], strict=False)
    )


def test_gap_is_a_warning_and_blocks_later_confirmation_policy() -> None:
    document = parse_pge_csv(
        _csv(
            "Electric usage,2026-01-01,00:00,00:14,0.1,kWh,,",
            "Electric usage,2026-01-01,00:30,00:44,0.1,kWh,,",
        )
    )
    assert {finding.code for finding in document.findings} == {"INTERVAL_GAP"}


def test_spring_forward_day_maps_to_23_contiguous_utc_hours() -> None:
    rows = []
    for hour in [0, 1, *range(3, 24)]:
        for minute in (0, 15, 30, 45):
            rows.append(
                f"Electric usage,2026-03-08,{hour:02d}:{minute:02d},"
                f"{hour:02d}:{minute + 14:02d},0.1,kWh,,"
            )
    document = parse_pge_csv(_csv(*rows))
    assert len(document.readings) == 92
    assert document.findings == ()


def test_duplicate_csv_interval_is_fatal() -> None:
    row = "Electric usage,2026-01-01,00:00,00:14,0.1,kWh,,"
    with pytest.raises(PgeCsvError) as raised:
        parse_pge_csv(_csv(row, row))
    assert raised.value.code == "DUPLICATE_INTERVALS"
