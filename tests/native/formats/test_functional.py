from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import warnings
from typing import Any, cast
from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    LoadOptions,
    ParseLimits,
    parse_document,
    render_document,
)
from pyowl_core.backends import native
from pyowl_core.cancellation import CancellationSource
from pyowl_core.exceptions import (
    BackendProtocolError,
    BackendUnavailableError,
    NativeBackendUnavailableWarning,
    OperationCancelledError,
    ResourceLimitError,
    UnsupportedSyntaxError,
)
from pyowl_core.extensions.swrl import SWRLRule
from pyowl_core.io.formats.functional import _render_node, parse_functional
from tests.generated.model.fixtures import model_fixtures
from tests.native.foundation._support import NativeTestExtension, load_extension
from tests.roundtrip.test_every_constructor import _source_document


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available or "parse-functional-v1" not in result.features:
        pytest.skip(result.reason or "native Functional parser capability is unavailable")
    return selected


def test_every_constructor_public_document_has_python_parity_and_native_provenance() -> None:
    source = render_document(_source_document(), format=DocumentFormat.FUNCTIONAL)
    python = parse_document(
        source,
        format=DocumentFormat.FUNCTIONAL,
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            preserve_source_map=True,
        ),
    )
    selected = parse_document(
        source,
        format=DocumentFormat.FUNCTIONAL,
        options=LoadOptions(
            backend=BackendPreference.NATIVE,
            preserve_source_map=True,
        ),
    )
    assert selected == python
    assert selected.document_fingerprint == python.document_fingerprint
    assert selected.source_map == python.source_map
    assert selected.origin_index == python.origin_index
    assert selected.provenance.backend == "native"
    assert selected.provenance.parser == "pyowl_core.backends.native"


@pytest.mark.parametrize(
    "source",
    (
        b"Ontology(SubClassOf(ObjectIntersectionOf(<urn:A> <urn:A>) <urn:B>))",
        b"Ontology(SubClassOf(ObjectUnionOf(<urn:A> <urn:A>) <urn:B>))",
        b"Ontology(DataPropertyRange(<urn:p> DataIntersectionOf(<urn:D> <urn:D>)))",
        b"Ontology(DataPropertyRange(<urn:p> DataUnionOf(<urn:D> <urn:D>)))",
        b"Ontology(DisjointClasses(<urn:A> <urn:A>))",
        b"Ontology(DisjointClasses(<urn:A> <urn:B> <urn:A> <urn:A>))",
        b"Ontology(DisjointClasses(<urn:A> <urn:B> <urn:A> <urn:A> <urn:B>))",
    ),
)
def test_duplicate_operand_canonicalization_has_native_parity(source: bytes) -> None:
    python = parse_document(
        source,
        format=DocumentFormat.FUNCTIONAL,
        options=LoadOptions(backend=BackendPreference.PYTHON, preserve_source_map=True),
    )
    selected = parse_document(
        source,
        format=DocumentFormat.FUNCTIONAL,
        options=LoadOptions(backend=BackendPreference.NATIVE, preserve_source_map=True),
    )

    assert selected == python
    assert selected.document_fingerprint == python.document_fingerprint
    assert selected.source_map == python.source_map
    assert selected.origin_index == python.origin_index


def test_anonymous_bom_unicode_and_swrl_match_reference_parser() -> None:
    rule = model_fixtures()[SWRLRule]
    source = (
        "\ufeffOntology(\n"
        '  Annotation(<urn:label> "café"@PT-br)\n'
        "  ClassAssertion(<urn:C> _:person)\n"
        "  SameIndividual(_:other _:person)\n"
        f"  {_render_node(rule)}\n"
        ")\n"
    ).encode()
    python = parse_functional(source, limits=ParseLimits(), allow_swrl=True)
    selected = native.parse_functional(source, allow_swrl=True)
    assert selected == python
    assert selected.occurrences == python.occurrences
    with pytest.raises(UnsupportedSyntaxError):
        native.parse_functional(source)


def test_unadvertised_formats_fallback_or_fail_before_parser_work() -> None:
    owlxml = b"""<?xml version='1.0'?>
<Ontology xmlns='http://www.w3.org/2002/07/owl#'
 xmlns:xsd='http://www.w3.org/2001/XMLSchema#'
 ontologyIRI='urn:native:owlxml'><Declaration><Class IRI='urn:C'/></Declaration></Ontology>"""
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        document = parse_document(
            owlxml,
            format=DocumentFormat.OWL_XML,
            options=LoadOptions(backend=BackendPreference.AUTO),
        )
    assert document.provenance.backend == "python"
    assert any(issubclass(item.category, NativeBackendUnavailableWarning) for item in observed)
    with pytest.raises(BackendUnavailableError):
        parse_document(
            owlxml,
            format=DocumentFormat.OWL_XML,
            options=LoadOptions(backend=BackendPreference.NATIVE),
        )


def test_auto_keeps_small_functional_documents_on_python_without_a_probe() -> None:
    with patch("pyowl_core.backends.dispatch.select_backend") as select:
        document = parse_document(
            b"Ontology(Declaration(Class(<urn:C>)))",
            format=DocumentFormat.FUNCTIONAL,
            options=LoadOptions(backend=BackendPreference.AUTO),
        )
    select.assert_not_called()
    assert document.provenance.backend == "python"


def test_auto_routes_large_functional_documents_to_native() -> None:
    body = " ".join(f"Declaration(Class(<urn:auto:C{index}>))" for index in range(8_000))
    source = f"Ontology({body})".encode()
    assert len(source) >= 256 * 1024
    document = parse_document(
        source,
        format=DocumentFormat.FUNCTIONAL,
        options=LoadOptions(backend=BackendPreference.AUTO),
    )
    assert document.provenance.backend == "native"


def test_limits_cancellation_and_hostile_framing_publish_no_partial_result(
    extension: NativeTestExtension,
) -> None:
    source = b"Ontology(Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>)))"
    with pytest.raises(ResourceLimitError):
        native.parse_functional(source, limits=ParseLimits(max_axioms=1))
    with pytest.raises(ResourceLimitError):
        native.parse_functional(
            source,
            limits=ParseLimits(max_source_bytes=len(source) - 1),
        )
    cancellation = CancellationSource()
    cancellation.cancel("test cancellation")
    with pytest.raises(OperationCancelledError):
        native.parse_functional(source, cancellation_token=cancellation.token)

    request = struct.pack("<8sHHQ", b"PYNFSS1\0", 1, 0, len(source)) + source
    parse_native = cast(Any, extension).parse_document
    for length in range(len(request)):
        with pytest.raises(extension._NativeError):
            parse_native(request[:length], b"")
    valid = parse_native(request, b"")
    for length in range(len(valid)):
        with pytest.raises(BackendProtocolError):
            native._decode_parsed_functional(valid[:length], ParseLimits())


def test_native_parse_call_releases_the_gil(extension: NativeTestExtension) -> None:
    body = " ".join(f"Declaration(Class(<urn:C{index}>))" for index in range(20_000))
    source = f"Ontology({body})".encode()
    request = struct.pack("<8sHHQ", b"PYNFSS1\0", 1, 0, len(source)) + source
    progress = 0
    running = threading.Event()

    def competitor() -> None:
        nonlocal progress
        running.set()
        while running.is_set():
            progress += 1

    thread = threading.Thread(target=competitor)
    thread.start()
    try:
        before = progress
        cast(Any, extension).parse_document(request, b"")
        assert progress > before
    finally:
        running.clear()
        thread.join(timeout=2)


def test_explicit_python_parse_never_imports_native_modules() -> None:
    script = """
import sys
from pyowl_core.backends.python import parse_document
from pyowl_core.config import BackendPreference, DocumentFormat, LoadOptions
parse_document(
    b'Ontology(Declaration(Class(<urn:C>)))',
    format=DocumentFormat.FUNCTIONAL,
    options=LoadOptions(backend=BackendPreference.PYTHON),
)
assert 'pyowl_core._native' not in sys.modules
assert 'pyowl_core.backends.native' not in sys.modules
"""
    environment = dict(os.environ)
    environment.pop("PYOWL_CORE_TEST_NATIVE_LIBRARY", None)
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
