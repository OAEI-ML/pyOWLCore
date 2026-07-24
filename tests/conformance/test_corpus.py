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
    MappingResolver,
    OntologySyntaxError,
    ParseLimits,
    ResourceLimitError,
    StructuralConstraintError,
    UnsupportedSyntaxError,
    load_snapshot,
    parse_document,
)
from tools.corpus.coverage import OUTPUT as COVERAGE_LOCK
from tools.corpus.coverage import render_coverage
from tools.corpus.manifest import LOCK as CORPUS_LOCK
from tools.corpus.manifest import render_lock, validate_manifest

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "tests" / "data" / "corpus"
OPTIONS = LoadOptions(backend=BackendPreference.PYTHON)
DOCUMENT_IRI = "https://example.org/conformance/ontology"
POSITIVE = {
    "functional": CORPUS / "w3c" / "functional" / "minimal.ofn",
    "owlxml": CORPUS / "w3c" / "owlxml" / "minimal.owx",
    "rdfxml": CORPUS / "w3c" / "rdfxml" / "minimal.rdf",
    "turtle": CORPUS / "w3c" / "turtle" / "minimal.ttl",
}


def test_provenance_hashes_and_generated_locks_are_current() -> None:
    artifacts = validate_manifest()
    assert len(artifacts) == 335
    assert CORPUS_LOCK.read_text(encoding="utf-8") == render_lock(artifacts)
    assert COVERAGE_LOCK.read_text(encoding="utf-8") == render_coverage()


def test_cross_syntax_positive_family_has_one_complete_structure() -> None:
    documents = {
        format: parse_document(
            path.read_bytes(),
            format=format,
            document_iri=DOCUMENT_IRI,
            options=OPTIONS,
        )
        for format, path in POSITIVE.items()
    }
    functional = documents["functional"]
    assert all(document == functional for document in documents.values())
    assert {document.document_fingerprint for document in documents.values()} == {
        functional.document_fingerprint
    }
    assert len(functional.axioms) == 4
    assert functional.rdf_mapping_report is None
    for format in ("rdfxml", "turtle"):
        report = documents[format].rdf_mapping_report
        assert report is not None and report.conformant
        assert report.consumed_triples == report.total_triples


def test_negative_and_hostile_corpus_has_stable_typed_outcomes() -> None:
    with pytest.raises(StructuralConstraintError) as arity:
        parse_document(
            (CORPUS / "w3c" / "functional" / "invalid-arity.ofn").read_bytes(),
            format="functional",
            options=OPTIONS,
        )
    assert arity.value.code == "STRUCTURAL_CONSTRAINT"

    with pytest.raises(OntologySyntaxError) as entity:
        parse_document(
            (CORPUS / "hostile" / "xml-entity.owx").read_bytes(),
            format="owlxml",
            options=OPTIONS,
        )
    assert entity.value.code == "XML_FORBIDDEN_CONSTRUCT"

    with pytest.raises(UnsupportedSyntaxError) as cycle:
        parse_document(
            (CORPUS / "hostile" / "turtle-cyclic-list.ttl").read_bytes(),
            format="turtle",
            document_iri="https://example.org/hostile",
            options=OPTIONS,
        )
    assert cycle.value.code == "RDF_MAPPING_UNSUPPORTED"

    limited = LoadOptions(
        backend=BackendPreference.PYTHON,
        limits=ParseLimits(max_nesting_depth=4),
    )
    with pytest.raises(ResourceLimitError) as depth:
        parse_document(
            (CORPUS / "hostile" / "deep-functional.ofn").read_bytes(),
            format="functional",
            options=limited,
        )
    assert depth.value.limit == "max_nesting_depth"
    assert depth.value.observed == 5


def test_errata_language_and_anonymous_identity_decisions_are_regressions() -> None:
    lexical_options = LoadOptions(
        backend=BackendPreference.PYTHON,
        preserve_source_map=True,
    )
    upper = parse_document(
        (CORPUS / "errata" / "language-upper.ofn").read_bytes(),
        format="functional",
        options=lexical_options,
    )
    lower = parse_document(
        (CORPUS / "errata" / "language-lower.ofn").read_bytes(),
        format="functional",
        options=lexical_options,
    )
    assert upper == lower
    assert upper.document_fingerprint == lower.document_fingerprint
    assertion = cast(m.AnnotationAssertion, next(upper.iter_axioms(m.AnnotationAssertion)))
    literal = assertion.value
    assert isinstance(literal, m.Literal) and literal.language == "en-gb"
    assert upper.source_map is not None and lower.source_map is not None
    assert upper.source_map.occurrences_for(literal)[0].lexical["language-tag"] == "EN-gb"
    lower_assertion = cast(
        m.AnnotationAssertion,
        next(lower.iter_axioms(m.AnnotationAssertion)),
    )
    assert isinstance(lower_assertion.value, m.Literal)
    assert (
        lower.source_map.occurrences_for(lower_assertion.value)[0].lexical["language-tag"]
        == "en-GB"
    )

    left = parse_document(
        (CORPUS / "errata" / "blank-left.ofn").read_bytes(),
        format="functional",
        options=OPTIONS,
    )
    renamed = parse_document(
        (CORPUS / "errata" / "blank-renamed.ofn").read_bytes(),
        format="functional",
        options=OPTIONS,
    )
    assert left == renamed
    assert left.document_fingerprint == renamed.document_fingerprint


def test_ordinary_two_document_import_cycle_is_legal_and_complete() -> None:
    root_bytes = (CORPUS / "imports" / "cycle-root.ofn").read_bytes()
    leaf_bytes = (CORPUS / "imports" / "cycle-leaf.ofn").read_bytes()
    resolver = MappingResolver(
        {
            "https://example.org/import/root": root_bytes,
            "https://example.org/import/leaf": leaf_bytes,
        }
    )
    snapshot = load_snapshot(
        root_bytes,
        document_iri="https://example.org/import/root",
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_STRICT,
            backend=BackendPreference.PYTHON,
        ),
        resolver=resolver,
    )
    assert snapshot.is_complete
    assert len(snapshot.documents) == 2
    assert len(snapshot.import_manifest.edges) == 2
    assert len(tuple(snapshot.iter_axioms(m.Declaration))) == 2


def test_wp09_suites_have_no_skip_or_expected_failure_escape_hatch() -> None:
    forbidden = (
        "pytest" + ".skip",
        "pytest" + ".xfail",
        "@pytest.mark." + "skip",
        "x" + "fail(",
    )
    suites = [ROOT / "tests" / name for name in ("conformance", "security", "fuzz")]
    offenders = []
    for suite in suites:
        for path in suite.rglob("*.py"):
            if path.name == "test_mutations.py":
                continue  # inherited WP06 file, audited by its handoff
            text = path.read_text(encoding="utf-8")
            if any(token in text for token in forbidden):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
