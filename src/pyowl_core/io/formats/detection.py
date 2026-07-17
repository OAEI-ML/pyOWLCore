"""Bounded deterministic ontology format selection."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from pyowl_core.config import DocumentFormat
from pyowl_core.document.provenance import DetectionBasis
from pyowl_core.exceptions import FormatDetectionError, FormatGuessWarning

SNIFF_BYTES = 64 * 1024

MEDIA_TYPES = {
    "application/rdf+xml": DocumentFormat.RDF_XML,
    "application/xml": None,
    "text/xml": None,
    "text/turtle": DocumentFormat.TURTLE,
    "application/x-turtle": DocumentFormat.TURTLE,
    "application/owl+xml": DocumentFormat.OWL_XML,
    "application/owl-functional": DocumentFormat.FUNCTIONAL,
    "text/owl-functional": DocumentFormat.FUNCTIONAL,
}

EXTENSIONS = {
    ".ttl": DocumentFormat.TURTLE,
    ".turtle": DocumentFormat.TURTLE,
    ".owx": DocumentFormat.OWL_XML,
    ".owlxml": DocumentFormat.OWL_XML,
    ".ofn": DocumentFormat.FUNCTIONAL,
    ".fss": DocumentFormat.FUNCTIONAL,
    ".fun": DocumentFormat.FUNCTIONAL,
    ".rdf": None,
    ".owl": None,
    ".xml": None,
}


@dataclass(frozen=True, slots=True)
class FormatDetection:
    format: DocumentFormat
    basis: DetectionBasis
    content_format: DocumentFormat | None
    extension_format: DocumentFormat | None


def coerce_format(value: DocumentFormat | str | None) -> DocumentFormat | None:
    if value is None or isinstance(value, DocumentFormat):
        return value
    if not isinstance(value, str):
        raise TypeError("format must be DocumentFormat, str, or None")
    normalized = value.strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "rdfxml": DocumentFormat.RDF_XML,
        "turtle": DocumentFormat.TURTLE,
        "ttl": DocumentFormat.TURTLE,
        "owlxml": DocumentFormat.OWL_XML,
        "functional": DocumentFormat.FUNCTIONAL,
        "functionalsyntax": DocumentFormat.FUNCTIONAL,
        "ofn": DocumentFormat.FUNCTIONAL,
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise ValueError(f"unknown ontology format {value!r}") from error


def detect_format(
    data: bytes,
    *,
    explicit: DocumentFormat | str | None = None,
    media_type: str | None = None,
    extension: str | None = None,
) -> FormatDetection:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    forced = coerce_format(explicit)
    strong = sniff_content(data[:SNIFF_BYTES])
    extension_format = _extension_format(extension)
    if forced is not None:
        return FormatDetection(forced, DetectionBasis.EXPLICIT, strong, extension_format)
    selected_media: DocumentFormat | None = None
    if media_type is not None:
        if not isinstance(media_type, str):
            raise TypeError("media_type must be str or None")
        bare = media_type.split(";", 1)[0].strip().lower()
        if bare not in MEDIA_TYPES:
            raise FormatDetectionError(
                f"unsupported authoritative media type {bare!r}",
                code="FORMAT_MEDIA_TYPE",
            )
        selected_media = MEDIA_TYPES[bare]
        if selected_media is not None:
            return FormatDetection(
                selected_media, DetectionBasis.MEDIA_TYPE, strong, extension_format
            )
    if strong is not None:
        if extension_format is not None and extension_format is not strong:
            warnings.warn(
                f"content indicates {strong.value}, not extension hint {extension_format.value}",
                FormatGuessWarning,
                stacklevel=2,
            )
        return FormatDetection(strong, DetectionBasis.CONTENT, strong, extension_format)
    if extension_format is not None:
        return FormatDetection(extension_format, DetectionBasis.EXTENSION, None, extension_format)
    raise FormatDetectionError(
        "ontology format is ambiguous; provide an explicit format",
        code="FORMAT_AMBIGUOUS",
    )


def sniff_content(data: bytes) -> DocumentFormat | None:
    probe = data
    if probe.startswith(b"\xef\xbb\xbf"):
        probe = probe[3:]
    probe = probe.lstrip()
    # Skip an XML declaration and leading XML comments without parsing hostile XML.
    if probe.startswith(b"<?xml"):
        end = probe.find(b"?>")
        if end < 0:
            return None
        probe = probe[end + 2 :].lstrip()
    while probe.startswith(b"<!--"):
        end = probe.find(b"-->")
        if end < 0:
            return None
        probe = probe[end + 3 :].lstrip()
    if probe.startswith(b"<"):
        head = probe[:4096]
        if re.search(rb"<(?:[A-Za-z_][\w.-]*:)?RDF(?:\s|>)", head):
            return DocumentFormat.RDF_XML
        if (
            re.search(rb"<(?:[A-Za-z_][\w.-]*:)?Ontology(?:\s|>)", head)
            and b"http://www.w3.org/2002/07/owl#" in head
        ):
            return DocumentFormat.OWL_XML
        # A leading IRIREF followed by Turtle punctuation is Turtle, not XML.
        if re.match(rb"<[^>\r\n]+>\s+(?:<|[A-Za-z_:])", probe):
            return DocumentFormat.TURTLE
        return None
    if re.match(rb"(?is)(?:Prefix\s*\(|Ontology\s*\()", probe):
        return DocumentFormat.FUNCTIONAL
    if re.match(rb"(?is)(?:@prefix|@base|prefix\s|base\s)", probe):
        return DocumentFormat.TURTLE
    if re.match(rb"(?:_:[A-Za-z]|\[|\()", probe):
        return DocumentFormat.TURTLE
    return None


def _extension_format(extension: str | None) -> DocumentFormat | None:
    if extension is None:
        return None
    if not isinstance(extension, str):
        raise TypeError("extension must be str or None")
    normalized = extension.lower()
    if normalized and not normalized.startswith("."):
        normalized = "." + normalized
    return EXTENSIONS.get(normalized)


__all__ = [
    "EXTENSIONS",
    "MEDIA_TYPES",
    "SNIFF_BYTES",
    "FormatDetection",
    "coerce_format",
    "detect_format",
    "sniff_content",
]
