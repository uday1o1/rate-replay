"""Compatibility import for the Milestone 0 feasibility module name."""

from ratereplay_ingestion.espi import (
    ALLOWED_RESOURCE_KINDS,
    ATOM,
    ESPI,
    MAX_XML_BYTES,
    MAX_XML_DEPTH,
    EspiDocument,
    EspiFinding,
    EspiParseError,
    EspiReading,
    parse_espi,
)

__all__ = [
    "ALLOWED_RESOURCE_KINDS",
    "ATOM",
    "ESPI",
    "MAX_XML_BYTES",
    "MAX_XML_DEPTH",
    "EspiDocument",
    "EspiFinding",
    "EspiParseError",
    "EspiReading",
    "parse_espi",
]
