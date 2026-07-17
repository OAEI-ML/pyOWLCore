from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest import mock

import pytest

from pyowl_core import (
    BackendPreference,
    DetectionBasis,
    DigestKind,
    DocumentFormat,
    FormatDetectionError,
    FormatGuessWarning,
    LoadOptions,
    ParseLimits,
    detect_format,
    parse_document,
    write_document,
)

PYTHON_OPTIONS = LoadOptions(backend=BackendPreference.PYTHON)
FUNCTIONAL = b"Ontology(<https://example.org/ontology>)\n"


class ChunkedBinary:
    def __init__(self, data: bytes, chunk_size: int = 3) -> None:
        self.data = data
        self.chunk_size = chunk_size
        self.offset = 0
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        if self.offset >= len(self.data):
            return b""
        end = min(len(self.data), self.offset + self.chunk_size)
        result = self.data[self.offset : end]
        self.offset = end
        return result


def test_detection_precedence_and_ambiguous_input() -> None:
    explicit = detect_format(
        b"@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        explicit="functional",
        extension=".ttl",
    )
    assert explicit.format is DocumentFormat.FUNCTIONAL
    assert explicit.basis is DetectionBasis.EXPLICIT

    media = detect_format(b"opaque", media_type="text/turtle")
    assert media.format is DocumentFormat.TURTLE
    assert media.basis is DetectionBasis.MEDIA_TYPE

    with pytest.warns(FormatGuessWarning):
        content = detect_format(FUNCTIONAL, extension=".ttl")
    assert content.format is DocumentFormat.FUNCTIONAL
    assert content.basis is DetectionBasis.CONTENT

    with pytest.raises(FormatDetectionError, match="ambiguous"):
        detect_format(b"not an ontology syntax")


def test_path_stream_and_text_source_contracts(tmp_path: Path) -> None:
    path = tmp_path / "document.ofn"
    path.write_bytes(FUNCTIONAL)
    from_path = parse_document(path, options=PYTHON_OPTIONS)
    assert from_path.provenance.acquisition_locator == str(path)
    assert from_path.provenance.digest_kind is DigestKind.EXACT_BYTES

    stream = ChunkedBinary(FUNCTIONAL)
    from_stream = parse_document(
        stream,
        format=DocumentFormat.FUNCTIONAL,
        document_iri="https://example.org/source",
        options=PYTHON_OPTIONS,
    )
    assert not stream.closed
    assert from_stream.provenance.source_sha256 == hashlib.sha256(FUNCTIONAL).digest()

    text = io.StringIO(FUNCTIONAL.decode("utf-8"))
    from_text = parse_document(
        text,
        format=DocumentFormat.FUNCTIONAL,
        document_iri="https://example.org/text",
        options=PYTHON_OPTIONS,
    )
    assert not text.closed
    assert from_text.provenance.digest_kind is DigestKind.NORMALIZED_TEXT
    assert from_text.provenance.decoded_codepoint_length == len(FUNCTIONAL.decode("utf-8"))

    with pytest.raises(ValueError, match="TextIO sources require"):
        parse_document(io.StringIO("Ontology()"), options=PYTHON_OPTIONS)


def test_parse_document_records_but_never_opens_imports() -> None:
    source = b"Ontology(Import(<file:///must-not-open.owl>))"
    with mock.patch("builtins.open", side_effect=AssertionError("unexpected import open")):
        document = parse_document(
            source,
            format=DocumentFormat.FUNCTIONAL,
            options=PYTHON_OPTIONS,
        )
    assert tuple(item.value for item in document.direct_imports) == ("file:///must-not-open.owl",)


def test_source_map_and_caller_owned_output_stream() -> None:
    options = LoadOptions(
        backend=BackendPreference.PYTHON,
        preserve_source_map=True,
        limits=ParseLimits(),
    )
    source = b"Prefix(ex:=<https://example.org/>)Ontology(Declaration(Class(ex:C)))"
    document = parse_document(source, format="functional", options=options)
    assert document.source_map is not None
    assert document.source_map.prefixes["ex"] == "https://example.org/"
    axiom = next(document.iter_axioms())
    occurrence = document.source_map.occurrences_for(axiom)
    assert len(occurrence) == 1
    assert occurrence[0].span is not None

    output = io.BytesIO()
    write_document(document, output, format="functional")
    assert not output.closed
    assert output.getvalue().startswith(b"Ontology(")


def test_functional_source_spans_treat_crlf_as_one_line_break() -> None:
    options = LoadOptions(
        backend=BackendPreference.PYTHON,
        preserve_source_map=True,
    )
    source = b"Ontology(\r\nDeclaration(Class(<https://example.org/C>))\r\n)"
    document = parse_document(source, format="functional", options=options)
    assert document.source_map is not None
    axiom = next(document.iter_axioms())
    occurrence = document.source_map.occurrences_for(axiom)[0]
    assert occurrence.span is not None
    assert occurrence.span.line_start == 2


def test_atomic_path_writer_publishes_parseable_document(tmp_path: Path) -> None:
    document = parse_document(FUNCTIONAL, format="functional", options=PYTHON_OPTIONS)
    output = tmp_path / "published.ofn"
    write_document(document, output, format="functional", atomic=True)
    assert parse_document(output, options=PYTHON_OPTIONS) == document
    assert not tuple(tmp_path.glob(".published.ofn.*.tmp"))
