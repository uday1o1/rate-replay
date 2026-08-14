import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_ingestion.espi_spike import EspiParseError, parse_espi

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"
SCHEMA = ROOT / "third_party/espi-schema/espi-4.0.xsd"


def _payload() -> bytes:
    return FIXTURE.read_bytes()


def test_independently_sourced_pacific_fixture_is_accepted() -> None:
    document = parse_espi(_payload(), schema_path=SCHEMA)
    assert document.interval_seconds == 3600
    assert document.timezone_offset_seconds == -28_800
    assert len(document.readings) > 24
    assert all(reading.energy_wh >= 0 for reading in document.readings)


def test_espi_parser_consumes_a_bounded_binary_stream() -> None:
    payload = _payload()

    class GuardedStream(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None and 0 < size <= 64 * 1024
            return super().read(size)

    document = parse_espi(GuardedStream(payload), schema_path=SCHEMA)
    assert document.source_hash == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("needle", "replacement", "code"),
    [
        (b"<commodity>1</commodity>", b"<commodity>0</commodity>", "WRONG_COMMODITY"),
        (
            b"<accumulationBehaviour>4</accumulationBehaviour>",
            b"<accumulationBehaviour>1</accumulationBehaviour>",
            "UNSUPPORTED_READING_SEMANTICS",
        ),
        (
            b'<link rel="related" href="ReadingType/07"/>',
            b'<link rel="related" href="ReadingType/missing"/>',
            "DANGLING_RELATIONSHIP",
        ),
        (
            b'<link rel="up" href="ReadingType"/>',
            b'<link rel="up" href="WrongCollection"/>',
            "DANGLING_RELATIONSHIP",
        ),
        (
            b'<link rel="related" href="ReadingType/07"/>',
            (
                b'<link rel="related" href="ReadingType/07"/>'
                b'<link rel="related" href="ReadingType/07"/>'
            ),
            "DUPLICATE_RELATIONSHIP",
        ),
    ],
)
def test_semantic_and_relationship_failures_are_stable(
    needle: bytes, replacement: bytes, code: str
) -> None:
    mutated = _payload().replace(needle, replacement, 1)
    assert mutated != _payload()
    with pytest.raises(EspiParseError) as raised:
        parse_espi(mutated)
    assert raised.value.code == code


def test_entity_and_external_reference_payloads_are_rejected() -> None:
    malicious = (ROOT / "data/fixtures/espi/malicious-external-entity.xml").read_bytes()
    with pytest.raises(EspiParseError) as raised:
        parse_espi(malicious)
    assert raised.value.code == "EXTERNAL_ENTITY_REFERENCE"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "EMPTY_FILE"),
        (b"not xml", "XML_SCHEMA_FAILURE"),
        (b'<feed xmlns="wrong"/>', "UNSUPPORTED_CONTENT_TYPE"),
        (b"x" * (10 * 1024 * 1024 + 1), "OVERSIZED_FILE"),
    ],
)
def test_malformed_and_bounded_payloads_fail_closed(payload: bytes, code: str) -> None:
    with pytest.raises(EspiParseError) as raised:
        parse_espi(payload)
    assert raised.value.code == code


def test_wrong_commodity_corpus_is_rejected_before_graph_use() -> None:
    payload = (ROOT / "data/fixtures/espi/invalid-wrong-commodity.xml").read_bytes()
    with pytest.raises(EspiParseError) as raised:
        parse_espi(payload)
    assert raised.value.code == "WRONG_COMMODITY"


def test_missing_schema_lock_fails_closed() -> None:
    with pytest.raises(EspiParseError) as raised:
        parse_espi(_payload(), schema_path=ROOT / "missing-schema.xsd")
    assert raised.value.code == "SCHEMA_LOCK_MISSING"


def test_nonintegral_espi_energy_is_never_rounded() -> None:
    mutated = _payload().replace(
        b"<powerOfTenMultiplier>0</powerOfTenMultiplier>",
        b"<powerOfTenMultiplier>-1</powerOfTenMultiplier>",
        1,
    )
    with pytest.raises(EspiParseError) as raised:
        parse_espi(mutated)
    assert raised.value.code == "NON_INTEGRAL_WATT_HOUR"


def test_entry_order_does_not_change_normalized_readings() -> None:
    payload = _payload()
    marker = b"<entry>"
    first_start = payload.index(marker)
    first_end = payload.index(b"</entry>", first_start) + len(b"</entry>")
    feed_end = payload.rindex(b"</feed>")
    first_entry = payload[first_start:first_end]
    reordered = (
        payload[:first_start] + payload[first_end:feed_end] + first_entry + payload[feed_end:]
    )
    assert parse_espi(payload).readings == parse_espi(reordered).readings


@pytest.mark.parametrize(
    ("field", "replacement", "code"),
    [
        ("flowDirection", "19", "UNSUPPORTED_READING_SEMANTICS"),
        ("kind", "37", "UNSUPPORTED_READING_SEMANTICS"),
        ("dataQualifier", "2", "UNSUPPORTED_READING_SEMANTICS"),
        ("timeAttribute", "1", "UNSUPPORTED_READING_SEMANTICS"),
        ("uom", "38", "UNSUPPORTED_READING_SEMANTICS"),
    ],
)
def test_every_unsupported_reading_semantic_fails_closed(
    field: str, replacement: str, code: str
) -> None:
    payload = _payload()
    opening = f"<{field}>".encode()
    reading_type_start = payload.index(b"<ReadingType")
    start = payload.index(opening, reading_type_start) + len(opening)
    end = payload.index(f"</{field}>".encode(), start)
    mutated = payload[:start] + replacement.encode() + payload[end:]
    with pytest.raises(EspiParseError) as raised:
        parse_espi(mutated)
    assert raised.value.code == code


def test_fifteen_minute_stream_is_admitted_but_gaps_are_reported() -> None:
    payload = (
        _payload()
        .replace(
            b"<intervalLength>3600</intervalLength>",
            b"<intervalLength>900</intervalLength>",
            1,
        )
        .replace(b"<duration>3600</duration>", b"<duration>900</duration>")
    )
    document = parse_espi(payload)
    assert document.interval_seconds == 900
    assert {finding.code for finding in document.findings} == {"INTERVAL_GAP"}


@pytest.mark.parametrize(
    ("second_start", "code"),
    [
        (1293868800, "DUPLICATE_INTERVALS"),
        (1293870600, "OVERLAPPING_INTERVALS"),
    ],
)
def test_duplicate_and_overlap_intervals_are_fatal(second_start: int, code: str) -> None:
    payload = _payload().replace(
        b"<start>1293872400</start>",
        f"<start>{second_start}</start>".encode(),
        1,
    )
    with pytest.raises(EspiParseError) as raised:
        parse_espi(payload)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("quality", "expected_code"),
    [("7", "MANUALLY_EDITED"), ("8", "ESTIMATED_USING_REFERENCE_DAY")],
)
def test_known_quality_codes_become_warnings(quality: str, expected_code: str) -> None:
    payload = _payload().replace(
        b"<timePeriod>",
        (
            b"<ReadingQuality><quality>"
            + quality.encode()
            + b"</quality></ReadingQuality><timePeriod>"
        ),
        1,
    )
    document = parse_espi(payload)
    assert {finding.code for finding in document.findings} == {expected_code}
    assert expected_code in document.readings[0].quality_flags


def test_unknown_quality_code_is_fatal() -> None:
    payload = _payload().replace(
        b"<timePeriod>",
        b"<ReadingQuality><quality>99</quality></ReadingQuality><timePeriod>",
        1,
    )
    with pytest.raises(EspiParseError) as raised:
        parse_espi(payload)
    assert raised.value.code == "UNSUPPORTED_READING_QUALITY"


def test_multiple_usage_points_are_fatal() -> None:
    payload = _payload()
    marker = b"<entry>"
    first_start = payload.index(marker)
    first_end = payload.index(b"</entry>", first_start) + len(b"</entry>")
    first_entry = payload[first_start:first_end].replace(b"UsagePoint/01", b"UsagePoint/02", 1)
    mutated = payload[:first_end] + first_entry + payload[first_end:]
    with pytest.raises(EspiParseError) as raised:
        parse_espi(mutated)
    assert raised.value.code == "MULTIPLE_USAGE_POINTS"


def test_entity_declaration_is_rejected_even_after_first_chunk() -> None:
    padding = b" " * (64 * 1024 + 1)
    payload = b'<feed xmlns="http://www.w3.org/2005/Atom">' + padding + b"<!ENTITY x 'y'></feed>"
    with pytest.raises(EspiParseError) as raised:
        parse_espi(payload)
    assert raised.value.code == "XML_ENTITY_EXPANSION"
