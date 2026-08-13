"""Canonical profile content serialization and hashing."""

from __future__ import annotations

import hashlib
import struct
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

DOMAIN_SEPARATOR = b"RateReplay.ProfileContent.v1"


class CanonicalEncodingError(ValueError):
    """A value cannot be represented by the frozen canonical encoding."""


class FlowDirection(StrEnum):
    IMPORT = "IMPORT"
    EXPORT = "EXPORT"
    REVERSE = "REVERSE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CanonicalReading:
    start_utc_ns: int
    duration_seconds: int
    energy_wh: int
    flow_direction: FlowDirection
    source_unit: str
    source_multiplier: int
    source_reading_type: str
    source_service_category: str
    source_commodity: str
    source_accumulation_behavior: str
    source_data_qualifier: str
    source_time_attribute: str
    source_local_time_parameters_hash: str | None
    source_timezone_offset_seconds: int | None
    source_dst_offset_seconds: int | None
    quality_flags: frozenset[str]


@dataclass(frozen=True, order=True, slots=True)
class CanonicalFinding:
    code: str
    severity: str
    field_path: str
    safe_value: str


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if value != normalized:
        raise CanonicalEncodingError("String input is not NFC-normalized")
    return normalized


def _int64(value: int) -> bytes:
    try:
        return struct.pack(">q", value)
    except struct.error as error:
        raise CanonicalEncodingError("Integer is outside the signed 64-bit range") from error


def _uint32(value: int) -> bytes:
    try:
        return struct.pack(">I", value)
    except struct.error as error:
        raise CanonicalEncodingError("Length is outside the unsigned 32-bit range") from error


def _text(value: str) -> bytes:
    encoded = _normalize(value).encode("utf-8")
    return _uint32(len(encoded)) + encoded


def _optional_text(value: str | None) -> bytes:
    return b"\x00" if value is None else b"\x01" + _text(value)


def _optional_int64(value: int | None) -> bytes:
    return b"\x00" if value is None else b"\x01" + _int64(value)


def _sequence(items: Iterable[bytes]) -> bytes:
    materialized = tuple(items)
    return _uint32(len(materialized)) + b"".join(materialized)


@dataclass(frozen=True, slots=True)
class CanonicalProfileContentV1:
    parser_contract_version: str
    adapter_fingerprint: str
    finding_policy_version: str
    confirmation_policy_version: str
    billing_period_start_utc_ns: int
    billing_period_end_utc_ns: int
    tariff_timezone: str
    interval_resolution_seconds: int
    readings: tuple[CanonicalReading, ...]
    findings: tuple[CanonicalFinding, ...]
    acknowledged_warning_ids: tuple[str, ...]

    def to_bytes(self) -> bytes:
        """Return the diagnostic canonical byte vector."""

        sorted_readings = tuple(
            sorted(self.readings, key=lambda item: (item.start_utc_ns, item.duration_seconds))
        )
        keys = [(reading.start_utc_ns, reading.duration_seconds) for reading in sorted_readings]
        if len(keys) != len(set(keys)):
            raise CanonicalEncodingError("Duplicate canonical interval key")
        if self.interval_resolution_seconds <= 0:
            raise CanonicalEncodingError("Interval resolution must be positive")
        if self.billing_period_end_utc_ns <= self.billing_period_start_utc_ns:
            raise CanonicalEncodingError("Billing period must be a nonempty half-open range")
        reading_bytes = (
            _int64(reading.start_utc_ns)
            + _int64(reading.duration_seconds)
            + _int64(reading.energy_wh)
            + _text(reading.flow_direction.value)
            + _text(reading.source_unit)
            + _int64(reading.source_multiplier)
            + _text(reading.source_reading_type)
            + _text(reading.source_service_category)
            + _text(reading.source_commodity)
            + _text(reading.source_accumulation_behavior)
            + _text(reading.source_data_qualifier)
            + _text(reading.source_time_attribute)
            + _optional_text(reading.source_local_time_parameters_hash)
            + _optional_int64(reading.source_timezone_offset_seconds)
            + _optional_int64(reading.source_dst_offset_seconds)
            + _sequence(_text(flag) for flag in sorted(reading.quality_flags))
            for reading in sorted_readings
        )
        finding_bytes = (
            _text(finding.code)
            + _text(finding.severity)
            + _text(finding.field_path)
            + _text(finding.safe_value)
            for finding in sorted(self.findings)
        )
        warning_ids = tuple(sorted(self.acknowledged_warning_ids))
        if len(warning_ids) != len(set(warning_ids)):
            raise CanonicalEncodingError("Duplicate acknowledged warning identity")
        fields = (
            _text(self.parser_contract_version)
            + _text(self.adapter_fingerprint)
            + _text(self.finding_policy_version)
            + _text(self.confirmation_policy_version)
            + _int64(self.billing_period_start_utc_ns)
            + _int64(self.billing_period_end_utc_ns)
            + _text(self.tariff_timezone)
            + _int64(self.interval_resolution_seconds)
            + _sequence(reading_bytes)
            + _sequence(finding_bytes)
            + _sequence(_text(warning_id) for warning_id in warning_ids)
        )
        return DOMAIN_SEPARATOR + b"\x00" + fields

    def sha256(self) -> str:
        """Return the lowercase SHA-256 content identity."""

        return hashlib.sha256(self.to_bytes()).hexdigest()
