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
