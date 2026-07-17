from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    MappedOntologySnapshot,
    OntologyDocument,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    open_snapshot,
    parse_document,
    render_document,
)


def _document(language: str) -> OntologyDocument:
    source = (
        "Ontology(<https://example.org/language> "
        "Declaration(AnnotationProperty(<https://example.org/p>)) "
        "AnnotationAssertion(<https://example.org/p> <https://example.org/s> "
        f'"value"@{language}))'
    ).encode()
    return parse_document(
        source,
        format=DocumentFormat.FUNCTIONAL,
        document_iri="https://example.org/language",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            preserve_source_map=True,
        ),
    )


@pytest.mark.parametrize("format", tuple(DocumentFormat))
def test_each_syntax_retains_original_language_tag_spelling(format: DocumentFormat) -> None:
    baseline = _document("en-gb")
    source = render_document(baseline, format=format).replace(b"en-gb", b"EN-gb")
    parsed = parse_document(
        source,
        format=format,
        document_iri="https://example.org/language",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            preserve_source_map=True,
        ),
    )
    assertion = cast(
        m.AnnotationAssertion,
        next(parsed.iter_axioms(m.AnnotationAssertion)),
    )
    assert isinstance(assertion.value, m.Literal)
    assert parsed.source_map is not None
    occurrences = parsed.source_map.occurrences_for(assertion.value)
    assert occurrences[0].lexical["language-tag"] == "EN-gb"
    assert parsed == baseline


def test_language_tag_spelling_is_provenance_only_across_wire_paths(tmp_path: Path) -> None:
    upper = _document("EN-gb")
    lower = _document("en-GB")
    assert upper == lower
    assert upper.document_fingerprint == lower.document_fingerprint

    upper_assertion = cast(
        m.AnnotationAssertion,
        next(upper.iter_axioms(m.AnnotationAssertion)),
    )
    lower_assertion = cast(
        m.AnnotationAssertion,
        next(lower.iter_axioms(m.AnnotationAssertion)),
    )
    assert isinstance(upper_assertion.value, m.Literal)
    assert isinstance(lower_assertion.value, m.Literal)
    assert upper_assertion.value.language == lower_assertion.value.language == "en-gb"
    assert upper.source_map is not None and lower.source_map is not None
    upper_literal = upper.source_map.occurrences_for(upper_assertion.value)
    lower_literal = lower.source_map.occurrences_for(lower_assertion.value)
    assert upper_literal[0].lexical["language-tag"] == "EN-gb"
    assert lower_literal[0].lexical["language-tag"] == "en-GB"
    assert upper.source_map.occurrences_for(upper_assertion)[0].lexical["language-tag"] == "EN-gb"

    options = LoadOptions(
        backend=BackendPreference.PYTHON,
        imports=ImportPolicy.IGNORE,
        preserve_source_map=True,
    )
    upper_snapshot = load_snapshot(upper, options=options)
    lower_snapshot = load_snapshot(lower, options=options)
    assert upper_snapshot.structural_fingerprint == lower_snapshot.structural_fingerprint
    upper_wire = encode_snapshot(upper_snapshot)
    assert upper_wire == encode_snapshot(lower_snapshot)

    decoded = decode_snapshot(upper_wire)
    assert decoded.structural_fingerprint == upper_snapshot.structural_fingerprint
    assert decoded.root.source_map is None
    assert encode_snapshot(decoded) == upper_wire

    path = tmp_path / "language.pyocore"
    path.write_bytes(upper_wire)
    mapped = open_snapshot(path)
    assert isinstance(mapped, MappedOntologySnapshot)
    try:
        assert mapped.structural_fingerprint == upper_snapshot.structural_fingerprint
        assert encode_snapshot(mapped) == upper_wire
    finally:
        mapped.close()
