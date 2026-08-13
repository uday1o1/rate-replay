"""Secure, relationship-aware ESPI parser feasibility implementation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Final

from lxml import etree
from ratereplay_domain.energy import EnergyAdmissionError, exact_watt_hours

ATOM: Final = "http://www.w3.org/2005/Atom"
ESPI: Final = "http://naesb.org/espi"
MAX_XML_BYTES: Final = 10 * 1024 * 1024
MAX_XML_DEPTH: Final = 64
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


@dataclass(frozen=True, slots=True)
class EspiDocument:
    source_hash: str
    interval_seconds: int
    timezone_offset_seconds: int
    dst_offset_seconds: int
    readings: tuple[EspiReading, ...]
    findings: tuple[EspiFinding, ...]


@dataclass(frozen=True, slots=True)
class _Entry:
    self_href: str
    kind: str
    content: etree._Element
    links: tuple[tuple[str, str], ...]


def _required_int(element: etree._Element, name: str) -> int:
    child = element.find(f"{{{ESPI}}}{name}")
    if child is None or child.text is None:
        raise EspiParseError("MISSING_READING_SEMANTIC", f"Missing {name}")
    try:
        return int(child.text)
    except ValueError as error:
        raise EspiParseError("INVALID_READING_SEMANTIC", f"Invalid integer {name}") from error


def _secure_root(payload: bytes) -> etree._Element:
    if not payload:
        raise EspiParseError("EMPTY_FILE", "XML payload is empty")
    if len(payload) > MAX_XML_BYTES:
        raise EspiParseError("OVERSIZED_FILE", "XML payload exceeds the locked size limit")
    upper_prefix = payload[:4096].upper()
    if b"<!DOCTYPE" in upper_prefix:
        raise EspiParseError(
            "EXTERNAL_ENTITY_REFERENCE", "Document type declarations are forbidden"
        )
    if b"<!ENTITY" in upper_prefix:
        raise EspiParseError("XML_ENTITY_EXPANSION", "Entity declarations are forbidden")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
        remove_comments=False,
    )
    try:
        root = etree.parse(BytesIO(payload), parser).getroot()
    except etree.XMLSyntaxError as error:
        raise EspiParseError("XML_SCHEMA_FAILURE", "Malformed XML") from error
    stack: list[tuple[etree._Element, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_XML_DEPTH:
            raise EspiParseError("EXCESSIVE_NESTING", "XML nesting exceeds the locked limit")
        stack.extend((child, depth + 1) for child in node if isinstance(child.tag, str))
    if root.tag != f"{{{ATOM}}}feed":
        raise EspiParseError("UNSUPPORTED_CONTENT_TYPE", "Expected an Atom feed")
    return root


def _parse_entries(root: etree._Element) -> dict[str, _Entry]:
    entries: dict[str, _Entry] = {}
    for atom_entry in root.findall(f"{{{ATOM}}}entry"):
        self_hrefs = [
            link.get("href")
            for link in atom_entry.findall(f"{{{ATOM}}}link")
            if link.get("rel") == "self" and link.get("href")
        ]
        if len(self_hrefs) != 1:
            raise EspiParseError("AMBIGUOUS_RELATIONSHIP", "Each entry requires one self link")
        content = atom_entry.find(f"{{{ATOM}}}content")
        resources = (
            [] if content is None else [child for child in content if isinstance(child.tag, str)]
        )
        if len(resources) != 1:
            raise EspiParseError("AMBIGUOUS_RELATIONSHIP", "Each entry requires one ESPI resource")
        resource = resources[0]
        namespace, kind = etree.QName(resource).namespace, etree.QName(resource).localname
        if namespace != ESPI:
            raise EspiParseError("UNSUPPORTED_READING_SEMANTICS", "Entry content is not ESPI")
        if kind not in ALLOWED_RESOURCE_KINDS:
            raise EspiParseError(
                "UNSUPPORTED_READING_SEMANTICS", f"Unsupported ESPI resource {kind}"
            )
        links = tuple(
            (link.get("rel", ""), link.get("href", ""))
            for link in atom_entry.findall(f"{{{ATOM}}}link")
            if link.get("href")
        )
        graph_links = tuple(link for link in links if link[0] in {"self", "up", "related"})
        if len(graph_links) != len(set(graph_links)):
            raise EspiParseError("DUPLICATE_RELATIONSHIP", "Duplicate Atom graph link")
        self_href = self_hrefs[0]
        if self_href is None:
            raise EspiParseError("AMBIGUOUS_RELATIONSHIP", "Self link href is missing")
        if self_href in entries:
            raise EspiParseError("DUPLICATE_RELATIONSHIP", "Duplicate self link")
        entries[self_href] = _Entry(self_href, kind, resource, links)
    if not entries:
        raise EspiParseError("EMPTY_FILE", "Atom feed contains no entries")
    return entries


def _validate_up_relationship(entry: _Entry) -> None:
    up_hrefs = [href.rstrip("/") for rel, href in entry.links if rel == "up"]
    if len(up_hrefs) != 1:
        raise EspiParseError("AMBIGUOUS_RELATIONSHIP", f"{entry.kind} requires one up link")
    expected = entry.self_href.rstrip("/").rsplit("/", 1)[0]
    if up_hrefs[0] != expected:
        raise EspiParseError(
            "DANGLING_RELATIONSHIP", f"{entry.kind} up link does not own its self link"
        )


def _related(entry: _Entry, entries: dict[str, _Entry], kind: str) -> tuple[_Entry, ...]:
    relation_hrefs = [href.rstrip("/") for rel, href in entry.links if rel == "related"]
    matches = tuple(
        candidate
        for href, candidate in entries.items()
        if candidate.kind == kind
        and any(
            href.rstrip("/") == relation or href.rstrip("/").startswith(relation + "/")
            for relation in relation_hrefs
        )
    )
    return matches


def _validate_resource_schema(resources: tuple[etree._Element, ...], schema_path: Path) -> None:
    if not schema_path.is_file():
        raise EspiParseError("SCHEMA_LOCK_MISSING", "Pinned ESPI schema is unavailable")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        schema_document = etree.parse(str(schema_path), parser)
        schema = etree.XMLSchema(schema_document)
    except (etree.XMLSchemaParseError, etree.XMLSyntaxError) as error:
        raise EspiParseError("SCHEMA_LOCK_INVALID", "Pinned ESPI schema cannot compile") from error
    for resource in resources:
        if not schema.validate(etree.ElementTree(resource)):
            raise EspiParseError("XML_SCHEMA_FAILURE", "ESPI resource fails the pinned schema")


def parse_espi(payload: bytes, *, schema_path: Path | None = None) -> EspiDocument:
    """Parse one ESPI Atom document and resolve its calculation stream."""

    root = _secure_root(payload)
    entries = _parse_entries(root)
    if schema_path is not None:
        _validate_resource_schema(tuple(entry.content for entry in entries.values()), schema_path)
    usage_points = tuple(entry for entry in entries.values() if entry.kind == "UsagePoint")
    if len(usage_points) != 1:
        raise EspiParseError("MULTIPLE_USAGE_POINTS", "Exactly one usage point is required")
    usage_point = usage_points[0]
    _validate_up_relationship(usage_point)
    service_category = usage_point.content.find(f"{{{ESPI}}}ServiceCategory")
    if service_category is None or _required_int(service_category, "kind") != 0:
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
    reading_type = reading_types[0].content
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
        actual = _required_int(reading_type, field)
        if actual != expected_value:
            code = "WRONG_COMMODITY" if field == "commodity" else "UNSUPPORTED_READING_SEMANTICS"
            raise EspiParseError(code, f"Unsupported {field} code")
    interval_seconds = _required_int(reading_type, "intervalLength")
    if interval_seconds not in {900, 3600}:
        raise EspiParseError(
            "UNSUPPORTED_READING_SEMANTICS", "Only 15-minute or hourly data is admitted"
        )
    multiplier = _required_int(reading_type, "powerOfTenMultiplier")
    local_time = local_time_parameters[0].content
    timezone_offset = _required_int(local_time, "tzOffset")
    dst_offset = _required_int(local_time, "dstOffset")
    if timezone_offset != -28_800 or dst_offset != 3_600:
        raise EspiParseError(
            "TIMEZONE_METADATA_CONFLICT", "Local time parameters are not Pacific time"
        )
    findings: list[EspiFinding] = []
    readings: list[EspiReading] = []
    for block_entry in interval_blocks:
        for index, interval in enumerate(block_entry.content.findall(f"{{{ESPI}}}IntervalReading")):
            time_period = interval.find(f"{{{ESPI}}}timePeriod")
            value_element = interval.find(f"{{{ESPI}}}value")
            if time_period is None or value_element is None or value_element.text is None:
                raise EspiParseError("MISSING_READING_SEMANTIC", "Interval reading is incomplete")
            duration = _required_int(time_period, "duration")
            start = _required_int(time_period, "start")
            if duration != interval_seconds:
                raise EspiParseError(
                    "MIXED_INTERVAL_DURATIONS", "Reading duration differs from ReadingType"
                )
            try:
                energy_wh = exact_watt_hours(
                    value_element.text,
                    source_unit="Wh",
                    power_of_ten_multiplier=multiplier,
                )
            except EnergyAdmissionError as error:
                raise EspiParseError(error.code, str(error)) from error
            for quality in interval.findall(f"{{{ESPI}}}ReadingQuality/{{{ESPI}}}quality"):
                if quality.text not in {"7", "8"}:
                    raise EspiParseError("UNSUPPORTED_READING_QUALITY", "Unknown reading quality")
                findings.append(
                    EspiFinding("KNOWN_READING_QUALITY", "WARNING", f"readings[{index}]")
                )
            readings.append(EspiReading(start, duration, energy_wh))
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
    return EspiDocument(
        source_hash=hashlib.sha256(payload).hexdigest(),
        interval_seconds=interval_seconds,
        timezone_offset_seconds=timezone_offset,
        dst_offset_seconds=dst_offset,
        readings=normalized,
        findings=tuple(
            sorted(set(findings), key=lambda finding: (finding.code, finding.field_path))
        ),
    )
