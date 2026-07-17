from __future__ import annotations

import io
from pathlib import Path

import pytest

from pyowl_core import (
    IRI,
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologySyntaxError,
    OptionConflictError,
    coerce_snapshot,
    load_snapshot,
    parse_document,
)

ROOT = b"Ontology(<urn:ontology:root> Declaration(Class(<urn:Class>)))"


def _options(*, format: DocumentFormat | None = None) -> LoadOptions:
    return LoadOptions(
        format=format,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    )


class _OneShotBinary:
    def __init__(self, data: bytes, *, chunk_size: int = 7) -> None:
        self._data = data
        self._chunk_size = chunk_size
        self.offset = 0
        self.eof_reads = 0
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        if self.offset >= len(self._data):
            self.eof_reads += 1
            if self.eof_reads > 1:
                raise AssertionError("stream was acquired more than once")
            return b""
        end = min(len(self._data), self.offset + self._chunk_size)
        result = self._data[self.offset : end]
        self.offset = end
        return result


class _OneShotText(io.StringIO):
    def __init__(self, data: str) -> None:
        super().__init__(data)
        self.eof_reads = 0

    def read(self, size: int = -1) -> str:
        result = super().read(size)
        if not result:
            self.eof_reads += 1
            if self.eof_reads > 1:
                raise AssertionError("text stream was acquired more than once")
        return result


class _Provider:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def owl_snapshot(self) -> object:
        self.calls += 1
        return self.snapshot


def test_binary_and_text_streams_bind_root_document_iri_once() -> None:
    binary = _OneShotBinary(ROOT)
    snapshot = load_snapshot(
        binary,
        document_iri="urn:document:binary",
        options=_options(),
    )
    assert snapshot.root.provenance.document_iri == IRI("urn:document:binary")
    assert binary.offset == len(ROOT)
    assert binary.eof_reads == 1
    assert not binary.closed

    text = _OneShotText(ROOT.decode("utf-8"))
    view = coerce_snapshot(
        text,
        document_iri=IRI("urn:document:text"),
        options=_options(format=DocumentFormat.FUNCTIONAL),
    )
    assert view.root.provenance.document_iri == IRI("urn:document:text")
    assert text.eof_reads == 1
    assert not text.closed


def test_missing_stream_metadata_fails_before_acquisition() -> None:
    binary = _OneShotBinary(ROOT)
    with pytest.raises(ValueError, match="stream sources require document_iri"):
        load_snapshot(binary, options=_options())
    assert binary.offset == 0
    assert binary.eof_reads == 0

    text = _OneShotText(ROOT.decode("utf-8"))
    with pytest.raises(ValueError, match="TextIO sources require explicit format"):
        load_snapshot(
            text,
            document_iri="urn:document:text",
            options=_options(),
        )
    assert text.tell() == 0
    assert text.eof_reads == 0


def test_path_and_bytes_defaults_are_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "root.ofn"
    path.write_bytes(ROOT)

    from_path = load_snapshot(path, options=_options())
    assert from_path.root.provenance.document_iri == IRI(path.absolute().as_uri())
    assert from_path.root.provenance.acquisition_locator == str(path)

    from_bytes = load_snapshot(ROOT, options=_options())
    assert from_bytes.root.provenance.document_iri is None

    explicitly_based_bytes = load_snapshot(
        ROOT,
        document_iri="urn:document:bytes",
        options=_options(),
    )
    assert explicitly_based_bytes.root.provenance.document_iri == IRI("urn:document:bytes")

    explicitly_based = load_snapshot(
        path,
        document_iri="urn:document:override",
        options=_options(),
    )
    assert explicitly_based.root.provenance.document_iri == IRI("urn:document:override")
    assert explicitly_based.root.provenance.acquisition_locator == str(path)


def test_document_iri_never_rebases_parsed_or_existing_inputs() -> None:
    document = parse_document(ROOT, options=_options())
    with pytest.raises(OptionConflictError) as parsed_conflict:
        load_snapshot(
            document,
            document_iri="urn:document:replacement",
            options=_options(),
        )
    assert parsed_conflict.value.code == "DOCUMENT_IRI_SOURCE_CONFLICT"

    snapshot = load_snapshot(document, options=_options())
    with pytest.raises(OptionConflictError) as view_conflict:
        coerce_snapshot(snapshot, document_iri="urn:document:replacement")
    assert view_conflict.value.code == "DOCUMENT_IRI_SOURCE_CONFLICT"

    provider = _Provider(snapshot)
    with pytest.raises(OptionConflictError) as provider_conflict:
        coerce_snapshot(  # type: ignore[arg-type]
            provider,
            document_iri="urn:document:replacement",
        )
    assert provider_conflict.value.code == "DOCUMENT_IRI_SOURCE_CONFLICT"
    assert provider.calls == 0


def test_stream_syntax_failure_is_not_retried_or_closed() -> None:
    source = _OneShotBinary(b"Ontology(")
    with pytest.raises(OntologySyntaxError):
        load_snapshot(
            source,
            document_iri="urn:document:invalid",
            options=_options(format=DocumentFormat.FUNCTIONAL),
        )
    assert source.offset == len(b"Ontology(")
    assert source.eof_reads == 1
    assert not source.closed


def test_invalid_document_iri_type_fails_before_stream_read() -> None:
    source = _OneShotBinary(ROOT)
    with pytest.raises(TypeError, match="document_iri must be IRI, str, or None"):
        coerce_snapshot(
            source,
            document_iri=object(),  # type: ignore[arg-type]
            options=_options(),
        )
    assert source.offset == 0
    assert source.eof_reads == 0
