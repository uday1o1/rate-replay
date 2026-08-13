from collections.abc import Callable
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ratereplay_domain.profile_hash import (
    CanonicalEncodingError,
    CanonicalFinding,
    CanonicalProfileContentV1,
    CanonicalReading,
    FlowDirection,
)


def _reading(start: int, energy: int) -> CanonicalReading:
    return CanonicalReading(
        start_utc_ns=start,
        duration_seconds=900,
        energy_wh=energy,
        flow_direction=FlowDirection.IMPORT,
        source_unit="Wh",
        source_multiplier=0,
        source_reading_type="interval_energy",
        source_service_category="electricity",
        source_commodity="electricity",
        source_accumulation_behavior="delta_data",
        source_data_qualifier="average",
        source_time_attribute="not_applicable",
        source_local_time_parameters_hash="a" * 64,
        source_timezone_offset_seconds=-28_800,
        source_dst_offset_seconds=3_600,
        quality_flags=frozenset(),
    )


def _profile(readings: tuple[CanonicalReading, ...]) -> CanonicalProfileContentV1:
    return CanonicalProfileContentV1(
        parser_contract_version="espi-v1",
        adapter_fingerprint="schema-sha256",
        finding_policy_version="finding-v1",
        confirmation_policy_version="confirmation-v1",
        billing_period_start_utc_ns=1_700_000_000_000_000_000,
        billing_period_end_utc_ns=1_700_000_001_800_000_000,
        tariff_timezone="America/Los_Angeles",
        interval_resolution_seconds=900,
        readings=readings,
        findings=(CanonicalFinding("SOURCE_ORDER", "WARNING", "entries", "normalized"),),
        acknowledged_warning_ids=("SOURCE_ORDER:entries",),
    )


def test_source_order_and_persistence_identities_are_excluded() -> None:
    first = _reading(1_700_000_000_000_000_000, 100)
    second = _reading(1_700_000_000_900_000_000, 200)
    assert _profile((first, second)).to_bytes().hex() == _profile((second, first)).to_bytes().hex()
    assert _profile((first, second)).sha256() == _profile((second, first)).sha256()


@given(profile_id=st.uuids(), reading_id=st.uuids())
def test_random_persistence_identities_do_not_enter_content_hash(
    profile_id: object, reading_id: object
) -> None:
    content = _profile((_reading(1_700_000_000_000_000_000, 100),))
    persisted_first = {"profile_id": profile_id, "reading_id": reading_id, "content": content}
    persisted_second = {
        "profile_id": "different-profile",
        "reading_id": "different-reading",
        "content": content,
    }
    first_content = persisted_first["content"]
    second_content = persisted_second["content"]
    assert isinstance(first_content, CanonicalProfileContentV1)
    assert isinstance(second_content, CanonicalProfileContentV1)
    assert first_content.sha256() == second_content.sha256()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda profile: replace(profile, parser_contract_version="espi-v2"),
        lambda profile: replace(profile, adapter_fingerprint="different-adapter"),
        lambda profile: replace(profile, finding_policy_version="finding-v2"),
        lambda profile: replace(profile, confirmation_policy_version="confirmation-v2"),
        lambda profile: replace(
            profile, billing_period_start_utc_ns=profile.billing_period_start_utc_ns - 1
        ),
        lambda profile: replace(
            profile, billing_period_end_utc_ns=profile.billing_period_end_utc_ns + 1
        ),
        lambda profile: replace(profile, tariff_timezone="UTC"),
        lambda profile: replace(profile, interval_resolution_seconds=3600),
        lambda profile: replace(profile, readings=(replace(profile.readings[0], energy_wh=101),)),
        lambda profile: replace(
            profile,
            findings=(CanonicalFinding("GAP", "FATAL", "readings", "present"),),
        ),
        lambda profile: replace(profile, acknowledged_warning_ids=("different",)),
    ],
)
def test_every_profile_field_mutation_changes_hash(
    mutation: Callable[[CanonicalProfileContentV1], CanonicalProfileContentV1],
) -> None:
    base = _profile((_reading(1_700_000_000_000_000_000, 100),))
    changed = mutation(base)
    assert changed.sha256() != base.sha256()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda reading: replace(reading, start_utc_ns=reading.start_utc_ns + 1),
        lambda reading: replace(reading, duration_seconds=reading.duration_seconds + 1),
        lambda reading: replace(reading, energy_wh=reading.energy_wh + 1),
        lambda reading: replace(reading, flow_direction=FlowDirection.EXPORT),
        lambda reading: replace(reading, source_unit="kWh"),
        lambda reading: replace(reading, source_multiplier=1),
        lambda reading: replace(reading, source_reading_type="changed"),
        lambda reading: replace(reading, source_service_category="changed"),
        lambda reading: replace(reading, source_commodity="changed"),
        lambda reading: replace(reading, source_accumulation_behavior="changed"),
        lambda reading: replace(reading, source_data_qualifier="changed"),
        lambda reading: replace(reading, source_time_attribute="changed"),
        lambda reading: replace(reading, source_local_time_parameters_hash="b" * 64),
        lambda reading: replace(reading, source_timezone_offset_seconds=-25_200),
        lambda reading: replace(reading, source_dst_offset_seconds=0),
        lambda reading: replace(reading, quality_flags=frozenset({"ESTIMATED"})),
    ],
)
def test_every_reading_field_mutation_changes_hash(
    mutation: Callable[[CanonicalReading], CanonicalReading],
) -> None:
    reading = _reading(1_700_000_000_000_000_000, 100)
    base = _profile((reading,))
    changed = _profile((mutation(reading),))
    assert changed.sha256() != base.sha256()


def test_duplicate_interval_key_fails() -> None:
    reading = _reading(1_700_000_000_000_000_000, 100)
    with pytest.raises(CanonicalEncodingError, match="Duplicate"):
        _profile((reading, replace(reading, energy_wh=200))).to_bytes()
