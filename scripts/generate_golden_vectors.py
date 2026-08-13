#!/usr/bin/env python3
"""Generate diagnostic vectors for the frozen canonical profile encoding."""

from __future__ import annotations

import json
from pathlib import Path

from ratereplay_domain.profile_hash import (
    CanonicalFinding,
    CanonicalProfileContentV1,
    CanonicalReading,
    FlowDirection,
)


def golden_profile() -> CanonicalProfileContentV1:
    return CanonicalProfileContentV1(
        parser_contract_version="espi-v1",
        adapter_fingerprint="adapter-sha256:0123456789abcdef",
        finding_policy_version="finding-policy-v1",
        confirmation_policy_version="confirmation-policy-v1",
        billing_period_start_utc_ns=1_782_885_600_000_000_000,
        billing_period_end_utc_ns=1_785_564_000_000_000_000,
        tariff_timezone="America/Los_Angeles",
        interval_resolution_seconds=900,
        readings=(
            CanonicalReading(
                start_utc_ns=1_782_885_600_000_000_000,
                duration_seconds=900,
                energy_wh=125,
                flow_direction=FlowDirection.IMPORT,
                source_unit="Wh",
                source_multiplier=0,
                source_reading_type="12",
                source_service_category="0",
                source_commodity="1",
                source_accumulation_behavior="4",
                source_data_qualifier="12",
                source_time_attribute="0",
                source_local_time_parameters_hash="sha256:feedface",
                source_timezone_offset_seconds=-28_800,
                source_dst_offset_seconds=3_600,
                quality_flags=frozenset(),
            ),
            CanonicalReading(
                start_utc_ns=1_782_886_500_000_000_000,
                duration_seconds=900,
                energy_wh=250,
                flow_direction=FlowDirection.IMPORT,
                source_unit="Wh",
                source_multiplier=0,
                source_reading_type="12",
                source_service_category="0",
                source_commodity="1",
                source_accumulation_behavior="4",
                source_data_qualifier="12",
                source_time_attribute="0",
                source_local_time_parameters_hash="sha256:feedface",
                source_timezone_offset_seconds=-28_800,
                source_dst_offset_seconds=3_600,
                quality_flags=frozenset({"ESTIMATED"}),
            ),
        ),
        findings=(
            CanonicalFinding("KNOWN_READING_QUALITY", "WARNING", "readings[1]", "ESTIMATED"),
        ),
        acknowledged_warning_ids=("warning:known-quality:1",),
    )


def main() -> None:
    profile = golden_profile()
    encoded = profile.to_bytes()
    payload = {
        "domain_separator_ascii": "RateReplay.ProfileContent.v1",
        "encoding": "CanonicalProfileContentV1 fixed-order length-prefixed binary",
        "expected_hex": encoded.hex(),
        "expected_sha256": profile.sha256(),
        "golden_id": "canonical-profile-v1-two-readings",
    }
    output = Path("data/golden/canonical-profile-content-v1.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
