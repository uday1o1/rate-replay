"""Secure streaming ESPI ingestion with relationship-aware normalization."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final

from lxml import etree
from ratereplay_domain.energy import EnergyAdmissionError, exact_watt_hours

ATOM: Final = "http://www.w3.org/2005/Atom"
ESPI: Final = "http://naesb.org/espi"
MAX_XML_BYTES: Final = 10 * 1024 * 1024
MAX_XML_DEPTH: Final = 64
XML_CHUNK_BYTES: Final = 64 * 1024
ALLOWED_RESOURCE_KINDS: Final = frozenset(
    {
        "ElectricPowerUsageSummary",
        "IntervalBlock",
        "LocalTimeParameters",
        "MeterReading",
        "ReadingType",
        "UsagePoint",
    }
)


class EspiParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EspiFinding:
    code: str
    severity: str
    field_path: str


@dataclass(frozen=True, slots=True)
class EspiReading:
    start_utc_seconds: int
    duration_seconds: int
    energy_wh: int
    quality_flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EspiDocument:
    source_hash: str
    interval_seconds: int
    timezone_offset_seconds: int
    dst_offset_seconds: int
    readings: tuple[EspiReading, ...]
    findings: tuple[EspiFinding, ...]


@dataclass(frozen=True, slots=True)
class _RawReading:
    start: str | None
    duration: str | None
    value: str | None
    quality_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Entry:
    self_href: str
    kind: str
    values: tuple[tuple[str, str], ...]
    readings: tuple[_RawReading, ...]
    links: tuple[tuple[str, str], ...]

    def value(self, name: str) -> str | None:
        matches = [value for key, value in self.values if key == name]
        if len(matches) > 1:
            raise EspiParseError("AMBIGUOUS_READING_SEMANTIC", f"Duplicate {name}")
        return matches[0] if matches else None


def _compile_schema(schema_path: Path | None) -> etree.XMLSchema | None:
    if schema_path is None:
        return None
    if not schema_path.is_file():
        raise EspiParseError("SCHEMA_LOCK_MISSING", "Pinned ESPI schema is unavailable")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        schema_document = etree.parse(str(schema_path), parser)
        return etree.XMLSchema(schema_document)
    except (etree.XMLSchemaParseError, etree.XMLSyntaxError) as error:
        raise EspiParseError("SCHEMA_LOCK_INVALID", "Pinned ESPI schema cannot compile") from error


def _text(element: etree._Element | None) -> str | None:
    return None if element is None or element.text is None else element.text.strip()


def _parse_atom_entry(atom_entry: etree._Element, schema: etree.XMLSchema | None) -> _Entry:
    links = tuple(
        (link.get("rel", ""), link.get("href", ""))
        for link in atom_entry.findall(f"{{{ATOM}}}link")
        if link.get("href")
    )
    graph_links = tuple(link for link in links if link[0] in {"self", "up", "related"})
    if len(graph_links) != len(set(graph_links)):
        raise EspiParseError("DUPLICATE_RELATIONSHIP", "Duplicate Atom graph link")
    self_hrefs = [href for relation, href in links if relation == "self"]
    if len(self_hrefs) != 1:
        raise EspiParseError("AMBIGUOUS_RELATIONSHIP", "Each entry requires one self link")

    content = atom_entry.find(f"{{{ATOM}}}content")
    resources = (
        [] if content is None else [child for child in content if isinstance(child.tag, str)]
    )
    if len(resources) != 1:
        raise EspiParseError("AMBIGUOUS_RELATIONSHIP", "Each entry requires one ESPI resource")
    resource = resources[0]
    qname = etree.QName(resource)
    if qname.namespace != ESPI:
        raise EspiParseError("UNSUPPORTED_READING_SEMANTICS", "Entry content is not ESPI")
    kind = qname.localname
    if kind not in ALLOWED_RESOURCE_KINDS:
        raise EspiParseError("UNSUPPORTED_READING_SEMANTICS", f"Unsupported resource {kind}")
    if schema is not None and not schema.validate(etree.ElementTree(resource)):
        raise EspiParseError("XML_SCHEMA_FAILURE", "ESPI resource fails the pinned schema")

    values: list[tuple[str, str]] = []
    if kind == "UsagePoint":
        service = resource.find(f"{{{ESPI}}}ServiceCategory/{{{ESPI}}}kind")
        if _text(service) is not None:
            values.append(("serviceCategory", _text(service) or ""))
    elif kind in {"ReadingType", "LocalTimeParameters"}:
        for child in resource:
            if isinstance(child.tag, str) and child.text is not None:
                values.append((etree.QName(child).localname, child.text.strip()))

    readings: list[_RawReading] = []
    if kind == "IntervalBlock":
        for interval in resource.findall(f"{{{ESPI}}}IntervalReading"):
            period = interval.find(f"{{{ESPI}}}timePeriod")
            readings.append(
                _RawReading(
                    start=_text(None if period is None else period.find(f"{{{ESPI}}}start")),
                    duration=_text(None if period is None else period.find(f"{{{ESPI}}}duration")),
                    value=_text(interval.find(f"{{{ESPI}}}value")),
                    quality_codes=tuple(
                        code
                        for quality in interval.findall(
                            f"{{{ESPI}}}ReadingQuality/{{{ESPI}}}quality"
                        )
                        if (code := _text(quality)) is not None
                    ),
                )
            )
    return _Entry(self_hrefs[0], kind, tuple(values), tuple(readings), links)


def _stream_entries(payload: bytes, schema: etree.XMLSchema | None) -> dict[str, _Entry]:
    if not payload:
        raise EspiParseError("EMPTY_FILE", "XML payload is empty")
    if len(payload) > MAX_XML_BYTES:
        raise EspiParseError("OVERSIZED_FILE", "XML payload exceeds the locked size limit")
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload:
        raise EspiParseError(
            "EXTERNAL_ENTITY_REFERENCE", "Document type declarations are forbidden"
        )
    if b"<!ENTITY" in upper_payload:
        raise EspiParseError("XML_ENTITY_EXPANSION", "Entity declarations are forbidden")

    parser = etree.XMLPullParser(
        events=("start", "end"),
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
        remove_comments=False,
    )
    entries: dict[str, _Entry] = {}
    depth = 0
    root_seen = False
    try:
        for offset in range(0, len(payload), XML_CHUNK_BYTES):
            parser.feed(payload[offset : offset + XML_CHUNK_BYTES])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
                    if depth > MAX_XML_DEPTH:
                        raise EspiParseError(
                            "EXCESSIVE_NESTING", "XML nesting exceeds the locked limit"
                        )
                    if not root_seen:
                        root_seen = True
                        if element.tag != f"{{{ATOM}}}feed":
                            raise EspiParseError(
                                "UNSUPPORTED_CONTENT_TYPE", "Expected an Atom feed"
                            )
                else:
                    if element.tag == f"{{{ATOM}}}entry":
                        entry = _parse_atom_entry(element, schema)
                        if entry.self_href in entries:
                            raise EspiParseError("DUPLICATE_RELATIONSHIP", "Duplicate self link")
                        entries[entry.self_href] = entry
                        element.clear(keep_tail=True)
                        parent = element.getparent()
                        if parent is not None:
                            while element.getprevious() is not None:
                                del parent[0]
                    depth -= 1
        parser.close()
    except EspiParseError:
        raise
    except etree.XMLSyntaxError as error:
        raise EspiParseError("XML_SCHEMA_FAILURE", "Malformed XML") from error
    if not entries:
        raise EspiParseError("EMPTY_FILE", "Atom feed contains no entries")
    return entries


def _required_int(entry: _Entry, name: str) -> int:
    raw = entry.value(name)
    if raw is None:
        raise EspiParseError("MISSING_READING_SEMANTIC", f"Missing {name}")
    try:
        return int(raw)
    except ValueError as error:
        raise EspiParseError("INVALID_READING_SEMANTIC", f"Invalid integer {name}") from error


def _validate_up_relationship(entry: _Entry) -> None:
    up_hrefs = [href.rstrip("/") for relation, href in entry.links if relation == "up"]
    if len(up_hrefs) != 1:
        raise EspiParseError("AMBIGUOUS_RELATIONSHIP", f"{entry.kind} requires one up link")
    expected = entry.self_href.rstrip("/").rsplit("/", 1)[0]
    if up_hrefs[0] != expected:
        raise EspiParseError(
            "DANGLING_RELATIONSHIP", f"{entry.kind} up link does not own its self link"
        )


def _related(entry: _Entry, entries: dict[str, _Entry], kind: str) -> tuple[_Entry, ...]:
    relation_hrefs = [href.rstrip("/") for relation, href in entry.links if relation == "related"]
    return tuple(
        candidate
        for href, candidate in entries.items()
        if candidate.kind == kind
        and any(
            href.rstrip("/") == relation or href.rstrip("/").startswith(relation + "/")
            for relation in relation_hrefs
        )
    )


def _normalized_readings(
    interval_blocks: tuple[_Entry, ...], *, interval_seconds: int, multiplier: int
) -> tuple[tuple[EspiReading, ...], tuple[EspiFinding, ...]]:
    readings: list[EspiReading] = []
    findings: list[EspiFinding] = []
    for block in interval_blocks:
        for raw in block.readings:
            if raw.start is None or raw.duration is None or raw.value is None:
                raise EspiParseError("MISSING_READING_SEMANTIC", "Interval is incomplete")
            try:
                start = int(raw.start)
                duration = int(raw.duration)
            except ValueError as error:
                raise EspiParseError("INVALID_READING_SEMANTIC", "Invalid interval time") from error
            if duration != interval_seconds:
                raise EspiParseError(
                    "MIXED_INTERVAL_DURATIONS", "Reading duration differs from ReadingType"
                )
            flags: set[str] = set()
            for quality in raw.quality_codes:
                quality_name = {
                    "7": "MANUALLY_EDITED",
                    "8": "ESTIMATED_USING_REFERENCE_DAY",
                }.get(quality)
                if quality_name is None:
                    raise EspiParseError("UNSUPPORTED_READING_QUALITY", "Unknown reading quality")
                flags.add(quality_name)
            try:
                energy_wh = exact_watt_hours(
                    raw.value,
                    source_unit="Wh",
                    power_of_ten_multiplier=multiplier,
                )
            except EnergyAdmissionError as error:
                raise EspiParseError(error.code, str(error)) from error
            field_path = f"readings[{start}]"
            for flag in flags:
                findings.append(EspiFinding(flag, "WARNING", field_path))
            readings.append(EspiReading(start, duration, energy_wh, frozenset(flags)))

    if not readings:
        raise EspiParseError("EMPTY_FILE", "No interval readings were resolved")
    source_order = [(reading.start_utc_seconds, reading.duration_seconds) for reading in readings]
    if source_order != sorted(source_order):
        findings.append(EspiFinding("NON_MONOTONIC_INTERVALS", "WARNING", "readings"))
    normalized = tuple(
        sorted(readings, key=lambda reading: (reading.start_utc_seconds, reading.duration_seconds))
    )
    keys = [(reading.start_utc_seconds, reading.duration_seconds) for reading in normalized]
    if len(keys) != len(set(keys)):
        raise EspiParseError("DUPLICATE_INTERVALS", "Duplicate interval identity")
    for previous, current in pairwise(normalized):
        previous_end = previous.start_utc_seconds + previous.duration_seconds
        if current.start_utc_seconds < previous_end:
            raise EspiParseError("OVERLAPPING_INTERVALS", "Intervals overlap")
        if current.start_utc_seconds > previous_end:
            findings.append(EspiFinding("INTERVAL_GAP", "WARNING", "readings"))
    return normalized, tuple(
        sorted(set(findings), key=lambda finding: (finding.code, finding.field_path))
    )


def parse_espi(payload: bytes, *, schema_path: Path | None = None) -> EspiDocument:
    """Stream one ESPI Atom document and resolve its calculation stream."""

    entries = _stream_entries(payload, _compile_schema(schema_path))
    usage_points = tuple(entry for entry in entries.values() if entry.kind == "UsagePoint")
    if len(usage_points) != 1:
        raise EspiParseError("MULTIPLE_USAGE_POINTS", "Exactly one usage point is required")
    usage_point = usage_points[0]
    _validate_up_relationship(usage_point)
    if _required_int(usage_point, "serviceCategory") != 0:
        raise EspiParseError("WRONG_COMMODITY", "Usage point is not electric service")

    meter_readings = _related(usage_point, entries, "MeterReading")
    if len(meter_readings) != 1:
        raise EspiParseError("AMBIGUOUS_RELATIONSHIP", "Usage point must resolve one meter stream")
    meter_reading = meter_readings[0]
    reading_types = _related(meter_reading, entries, "ReadingType")
    interval_blocks = _related(meter_reading, entries, "IntervalBlock")
    local_time_parameters = _related(usage_point, entries, "LocalTimeParameters")
    if len(reading_types) != 1 or not interval_blocks or len(local_time_parameters) != 1:
        raise EspiParseError(
            "DANGLING_RELATIONSHIP", "Required ESPI relation is missing or ambiguous"
        )
    for related_entry in (meter_reading, *reading_types, *interval_blocks, *local_time_parameters):
        _validate_up_relationship(related_entry)

    reading_type = reading_types[0]
    expected = {
        "accumulationBehaviour": 4,
        "commodity": 1,
        "dataQualifier": 12,
        "flowDirection": 1,
        "kind": 12,
        "timeAttribute": 0,
        "uom": 72,
    }
    for field, expected_value in expected.items():
        if _required_int(reading_type, field) != expected_value:
            code = "WRONG_COMMODITY" if field == "commodity" else "UNSUPPORTED_READING_SEMANTICS"
            raise EspiParseError(code, f"Unsupported {field} code")
    interval_seconds = _required_int(reading_type, "intervalLength")
    if interval_seconds not in {900, 3600}:
        raise EspiParseError(
            "UNSUPPORTED_READING_SEMANTICS", "Only 15-minute or hourly data is admitted"
        )
    multiplier = _required_int(reading_type, "powerOfTenMultiplier")

    local_time = local_time_parameters[0]
    timezone_offset = _required_int(local_time, "tzOffset")
    dst_offset = _required_int(local_time, "dstOffset")
    if timezone_offset != -28_800 or dst_offset != 3_600:
        raise EspiParseError(
            "TIMEZONE_METADATA_CONFLICT", "Local time parameters are not Pacific time"
        )
    readings, findings = _normalized_readings(
        interval_blocks,
        interval_seconds=interval_seconds,
        multiplier=multiplier,
    )
    return EspiDocument(
        source_hash=hashlib.sha256(payload).hexdigest(),
        interval_seconds=interval_seconds,
        timezone_offset_seconds=timezone_offset,
        dst_offset_seconds=dst_offset,
        readings=readings,
        findings=findings,
    )
