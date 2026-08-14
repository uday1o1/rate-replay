from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from ratereplay_domain.profile_hash import CanonicalFinding, FlowDirection
from ratereplay_ingestion.espi import parse_espi
from ratereplay_ingestion.normalize import (
    ConfirmationError,
    NormalizedDraft,
    confirm_draft,
    normalize_espi,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"


def _draft() -> NormalizedDraft:
    return normalize_espi(parse_espi(FIXTURE.read_bytes()))


def _full_period(draft: NormalizedDraft) -> tuple[int, int]:
    start = draft.readings[0].start_utc_ns
    end = draft.readings[-1].start_utc_ns + draft.readings[-1].duration_seconds * 1_000_000_000
    return start, end


def test_complete_period_confirms_to_stable_canonical_content() -> None:
    draft = _draft()
    start, end = _full_period(draft)
    profile = confirm_draft(
        draft,
        billing_period_start_utc_ns=start,
        billing_period_end_utc_ns=end,
        acknowledged_warning_ids=(),
        pge_service_attested=True,
    )
    assert (
        profile.sha256()
        == confirm_draft(
            replace(draft, source_hash="different-upload-object-hash"),
            billing_period_start_utc_ns=start,
            billing_period_end_utc_ns=end,
            acknowledged_warning_ids=(),
            pge_service_attested=True,
        ).sha256()
    )
    assert all(reading.flow_direction is FlowDirection.IMPORT for reading in profile.readings)


def test_incomplete_period_and_missing_attestation_cannot_confirm() -> None:
    draft = _draft()
    start, end = _full_period(draft)
    with pytest.raises(ConfirmationError) as incomplete:
        confirm_draft(
            draft,
            billing_period_start_utc_ns=start,
            billing_period_end_utc_ns=end + 1_000_000_000,
            acknowledged_warning_ids=(),
            pge_service_attested=True,
        )
    assert incomplete.value.code == "INCOMPLETE_BILLING_PERIOD"
    with pytest.raises(ConfirmationError) as unattested:
        confirm_draft(
            draft,
            billing_period_start_utc_ns=start,
            billing_period_end_utc_ns=end,
            acknowledged_warning_ids=(),
            pge_service_attested=False,
        )
    assert unattested.value.code == "PGE_SERVICE_ATTESTATION_REQUIRED"


def test_every_warning_must_be_acknowledged_by_stable_identity() -> None:
    draft = _draft()
    warned = replace(
        draft,
        findings=(CanonicalFinding("INTERVAL_GAP", "WARNING", "readings[1]", ""),),
    )
    start, end = _full_period(warned)
    with pytest.raises(ConfirmationError) as raised:
        confirm_draft(
            warned,
            billing_period_start_utc_ns=start,
            billing_period_end_utc_ns=end,
            acknowledged_warning_ids=(),
            pge_service_attested=True,
        )
    assert raised.value.code == "WARNING_ACKNOWLEDGEMENT_MISMATCH"
    accepted = confirm_draft(
        warned,
        billing_period_start_utc_ns=start,
        billing_period_end_utc_ns=end,
        acknowledged_warning_ids=warned.warning_ids,
        pge_service_attested=True,
    )
    assert accepted.acknowledged_warning_ids == warned.warning_ids


def test_entry_order_and_atom_entry_ids_do_not_change_profile_content_hash() -> None:
    payload = FIXTURE.read_bytes()
    marker = b"<entry>"
    first_start = payload.index(marker)
    first_end = payload.index(b"</entry>", first_start) + len(b"</entry>")
    feed_end = payload.rindex(b"</feed>")
    first_entry = payload[first_start:first_end]
    reordered = (
        payload[:first_start] + payload[first_end:feed_end] + first_entry + payload[feed_end:]
    )
    changed_source_ids = reordered.replace(b"urn:uuid:", b"urn:test:")

    original = normalize_espi(parse_espi(payload))
    changed = normalize_espi(parse_espi(changed_source_ids))
    original_start, original_end = _full_period(original)
    changed_start, changed_end = _full_period(changed)
    original_profile = confirm_draft(
        original,
        billing_period_start_utc_ns=original_start,
        billing_period_end_utc_ns=original_end,
        acknowledged_warning_ids=original.warning_ids,
        pge_service_attested=True,
    )
    changed_profile = confirm_draft(
        changed,
        billing_period_start_utc_ns=changed_start,
        billing_period_end_utc_ns=changed_end,
        acknowledged_warning_ids=changed.warning_ids,
        pge_service_attested=True,
    )
    assert changed_profile.sha256() == original_profile.sha256()
