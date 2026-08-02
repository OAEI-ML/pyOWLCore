from __future__ import annotations

from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    LoadOptions,
    ParseLimits,
    RDFMappingReport,
    UnsupportedSyntaxError,
    parse_document,
)
from pyowl_core.backends import native
from tests.native.foundation._support import NativeTestExtension, load_extension

RDFXML_SOURCE = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:e="urn:evidence:">
  <rdf:Description rdf:about="urn:subject">
    <e:literal>value</e:literal>
    <e:resource rdf:resource="urn:object"/>
    <e:blank rdf:nodeID="object"/>
  </rdf:Description>
</rdf:RDF>
"""
TURTLE_SOURCE = b"""\
@prefix e: <urn:evidence:> .
<urn:subject> e:literal "value" ;
              e:resource <urn:object> ;
              e:blank _:object .
"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        pytest.skip(result.reason or "native backend unavailable")
    return selected


def _options(
    backend: BackendPreference,
    *,
    partial: bool,
) -> LoadOptions:
    return LoadOptions(
        backend=backend,
        limits=ParseLimits(max_diagnostics=3),
        allow_partial_rdf_mapping=partial,
    )


@pytest.mark.parametrize(
    ("format", "source", "native_entrypoint"),
    (
        (DocumentFormat.RDF_XML, RDFXML_SOURCE, "_parse_rdfxml_retained_v2"),
        (DocumentFormat.TURTLE, TURTLE_SOURCE, "_parse_turtle_retained_v2"),
    ),
)
def test_forced_native_strict_report_matches_python_and_partial_without_reparse(
    format: DocumentFormat,
    source: bytes,
    native_entrypoint: str,
) -> None:
    with pytest.raises(UnsupportedSyntaxError) as python_error:
        parse_document(
            source,
            format=format,
            options=_options(BackendPreference.PYTHON, partial=False),
        )
    python_report = python_error.value.rdf_mapping_report
    assert isinstance(python_report, RDFMappingReport)

    real_parse = getattr(native, native_entrypoint)
    with (
        patch.object(native, native_entrypoint, wraps=real_parse) as native_parse,
        pytest.raises(UnsupportedSyntaxError) as native_error,
    ):
        parse_document(
            source,
            format=format,
            options=_options(BackendPreference.NATIVE, partial=False),
        )
    assert native_parse.call_count == 1
    strict_report = native_error.value.rdf_mapping_report
    assert isinstance(strict_report, RDFMappingReport)
    assert strict_report == python_report
    assert strict_report.total_triples == 3
    assert strict_report.consumed_triples == 0
    assert strict_report.dropped_triples == 3
    assert {item.object_kind for item in strict_report.unconsumed} == {
        "blank",
        "iri",
        "literal",
    }

    with patch.object(native, native_entrypoint, wraps=real_parse) as native_parse:
        partial = parse_document(
            source,
            format=format,
            options=_options(BackendPreference.NATIVE, partial=True),
        )
    assert native_parse.call_count == 1
    assert partial.rdf_mapping_report == strict_report


def test_forced_native_strict_report_remains_usable_after_failed_load_cleanup() -> None:
    with pytest.raises(UnsupportedSyntaxError) as caught:
        parse_document(
            RDFXML_SOURCE,
            format=DocumentFormat.RDF_XML,
            options=_options(BackendPreference.NATIVE, partial=False),
        )

    report = caught.value.rdf_mapping_report
    assert report is not None
    assert tuple(item.predicate for item in report.unconsumed) == (
        "urn:evidence:blank",
        "urn:evidence:literal",
        "urn:evidence:resource",
    )


def test_forced_native_mapping_evidence_matches_python_credential_redaction() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:e="https://predicate-user:predicate-secret@example.org/">
  <rdf:Description rdf:about="https://subject-user:subject-secret@example.org/s">
    <e:value rdf:resource="https://object-user:object-secret@example.org/o"/>
  </rdf:Description>
</rdf:RDF>
"""
    reports: list[RDFMappingReport] = []
    for backend in (BackendPreference.PYTHON, BackendPreference.NATIVE):
        with pytest.raises(UnsupportedSyntaxError) as caught:
            parse_document(
                source,
                format=DocumentFormat.RDF_XML,
                options=_options(backend, partial=False),
            )
        assert caught.value.rdf_mapping_report is not None
        reports.append(caught.value.rdf_mapping_report)

    assert reports[1] == reports[0]
    evidence = reports[1].unconsumed[0]
    rendered = " ".join((evidence.subject, evidence.predicate, evidence.object))
    assert "secret" not in rendered
    assert rendered.count("<redacted>@") == 3
