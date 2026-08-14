"""Adapter-independent draft normalization and confirmation policy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ratereplay_domain.profile_hash import (
    CanonicalFinding,
    CanonicalProfileContentV1,
    CanonicalReading,
    FlowDirection,
)

from ratereplay_ingestion.espi import EspiDocument
from ratereplay_ingestion.pge_csv import PgeCsvDocument

PARSER_CONTRACT_VERSION = "interval-import-parser-v1"
FINDING_POLICY_VERSION = "import-finding-policy-v1"
CONFIRMATION_POLICY_VERSION = "profile-confirmation-policy-v1"


class ConfirmationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NormalizedDraft:
    source_hash: str
    adapter_fingerprint: str
    tariff_timezone: str
    interval_resolution_seconds: int
    readings: tuple[CanonicalReading, ...]
    findings: tuple[CanonicalFinding, ...]

    @property
    def warning_ids(self) -> tuple[str, ...]:
        return tuple(
            _warning_id(finding) for finding in self.findings if finding.severity == "WARNING"
        )


def _warning_id(finding: CanonicalFinding) -> str:
    payload = "\x00".join((finding.code, finding.severity, finding.field_path, finding.safe_value))
    return hashlib.sha256(b"RateReplay.ImportWarning.v1\x00" + payload.encode()).hexdigest()


def _local_time_hash(timezone_offset: int, dst_offset: int) -> str:
    payload = f"{timezone_offset}\x00{dst_offset}".encode()
    return hashlib.sha256(b"RateReplay.LocalTimeParameters.v1\x00" + payload).hexdigest()


def normalize_espi(document: EspiDocument) -> NormalizedDraft:
    local_time_hash = _local_time_hash(
        document.timezone_offset_seconds,
        document.dst_offset_seconds,
    )
    readings = tuple(
        CanonicalReading(
            start_utc_ns=reading.start_utc_seconds * 1_000_000_000,
            duration_seconds=reading.duration_seconds,
            energy_wh=reading.energy_wh,
            flow_direction=FlowDirection.IMPORT,
            source_unit="Wh",
            source_multiplier=0,
            source_reading_type="12",
            source_service_category="0",
            source_commodity="1",
            source_accumulation_behavior="4",
            source_data_qualifier="12",
            source_time_attribute="0",
            source_local_time_parameters_hash=local_time_hash,
            source_timezone_offset_seconds=document.timezone_offset_seconds,
            source_dst_offset_seconds=document.dst_offset_seconds,
            quality_flags=reading.quality_flags,
        )
        for reading in document.readings
    )
    findings = tuple(
        CanonicalFinding(finding.code, finding.severity, finding.field_path, "")
        for finding in document.findings
    )
    return NormalizedDraft(
        source_hash=document.source_hash,
        adapter_fingerprint="espi-4.0-download-my-data-v1",
        tariff_timezone="America/Los_Angeles",
        interval_resolution_seconds=document.interval_seconds,
        readings=readings,
        findings=findings,
    )


def normalize_pge_csv(document: PgeCsvDocument) -> NormalizedDraft:
    readings = tuple(
        CanonicalReading(
            start_utc_ns=reading.start_utc_seconds * 1_000_000_000,
            duration_seconds=reading.duration_seconds,
            energy_wh=reading.energy_wh,
            flow_direction=FlowDirection.IMPORT,
            source_unit="kWh",
            source_multiplier=0,
            source_reading_type="PROVIDER_INTERVAL_ENERGY",
            source_service_category="ELECTRICITY",
            source_commodity="ELECTRICITY",
            source_accumulation_behavior="DELTA_DATA",
            source_data_qualifier="NORMAL",
            source_time_attribute="NOT_APPLICABLE",
            source_local_time_parameters_hash=None,
            source_timezone_offset_seconds=None,
            source_dst_offset_seconds=None,
            quality_flags=frozenset(),
        )
        for reading in document.readings
    )
    findings = tuple(
        CanonicalFinding(finding.code, finding.severity, finding.field_path, "")
        for finding in document.findings
    )
    return NormalizedDraft(
        source_hash=document.source_hash,
        adapter_fingerprint=document.adapter_fingerprint,
        tariff_timezone=document.timezone,
        interval_resolution_seconds=document.interval_seconds,
        readings=readings,
        findings=findings,
    )


def confirm_draft(
    draft: NormalizedDraft,
    *,
    billing_period_start_utc_ns: int,
    billing_period_end_utc_ns: int,
    acknowledged_warning_ids: tuple[str, ...],
    pge_service_attested: bool,
) -> CanonicalProfileContentV1:
    """Confirm only a complete, disjoint, one-resolution half-open period."""

    if not pge_service_attested:
        raise ConfirmationError(
            "PGE_SERVICE_ATTESTATION_REQUIRED",
            "Confirmation requires the PG&E service attestation.",
        )
    required_warnings = set(draft.warning_ids)
    acknowledged = set(acknowledged_warning_ids)
    if len(acknowledged) != len(acknowledged_warning_ids) or acknowledged != required_warnings:
        raise ConfirmationError(
            "WARNING_ACKNOWLEDGEMENT_MISMATCH",
            "Every current nonfatal warning must be acknowledged exactly once.",
        )
    selected = tuple(
        reading
        for reading in draft.readings
        if reading.start_utc_ns >= billing_period_start_utc_ns
        and reading.start_utc_ns + reading.duration_seconds * 1_000_000_000
        <= billing_period_end_utc_ns
    )
    if not selected:
        raise ConfirmationError("INCOMPLETE_BILLING_PERIOD", "Selected period has no readings.")
    expected_start = billing_period_start_utc_ns
    for reading in selected:
        if reading.duration_seconds != draft.interval_resolution_seconds:
            raise ConfirmationError(
                "MIXED_INTERVAL_DURATIONS", "Selected period mixes interval resolutions."
            )
        if reading.start_utc_ns != expected_start:
            raise ConfirmationError(
                "INCOMPLETE_BILLING_PERIOD", "Selected period contains a gap or overlap."
            )
        expected_start += reading.duration_seconds * 1_000_000_000
    if expected_start != billing_period_end_utc_ns:
        raise ConfirmationError(
            "INCOMPLETE_BILLING_PERIOD", "Selected period is not completely covered."
        )
    return CanonicalProfileContentV1(
        parser_contract_version=PARSER_CONTRACT_VERSION,
        adapter_fingerprint=draft.adapter_fingerprint,
        finding_policy_version=FINDING_POLICY_VERSION,
        confirmation_policy_version=CONFIRMATION_POLICY_VERSION,
        billing_period_start_utc_ns=billing_period_start_utc_ns,
        billing_period_end_utc_ns=billing_period_end_utc_ns,
        tariff_timezone=draft.tariff_timezone,
        interval_resolution_seconds=draft.interval_resolution_seconds,
        readings=selected,
        findings=draft.findings,
        acknowledged_warning_ids=tuple(sorted(acknowledged_warning_ids)),
    )
