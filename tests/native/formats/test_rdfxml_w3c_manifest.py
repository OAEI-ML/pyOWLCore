from __future__ import annotations

import hashlib
import itertools
import os
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

try:
    import tomllib  # type: ignore[import-untyped, unused-ignore]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, import-untyped, unused-ignore]

from pyowl_core.backends import native
from pyowl_core.exceptions import OntologySyntaxError
from pyowl_core.io.formats.common import ParseContext
from pyowl_core.io.formats.rdf import (
    RDFIRI,
    RDFBlank,
    RDFGraph,
    RDFLiteral,
    RDFResource,
    RDFTerm,
    Triple,
)
from pyowl_core.io.formats.rdfxml import (
    RDFXMLGraphParser,
    _decode_xml_source,
    _has_forbidden_xml_markup,
    _reserved_xml_attribute_names,
    _validate_xml_envelope,
)
from pyowl_core.io.formats.turtle import TurtleParser
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI
from tests.native.foundation._support import NativeTestExtension, load_extension

CORPUS = Path(__file__).parents[2] / "data" / "corpus" / "w3c" / "rdfxml" / "rdf11"
UPSTREAM = CORPUS / "UPSTREAM.toml"
MANIFEST = CORPUS / "manifest.ttl"
ASSUMED_BASE = "https://w3c.github.io/rdf-tests/rdf/rdf11/rdf-xml/"
_AGGREGATE_DOMAIN = b"pyowl-core:w3c-rdfxml-corpus:v1\x00"
_NATIVE_SYNTAX_ERRORS = frozenset(
    ("NATIVE_RDFXML_SYNTAX", "NATIVE_XML_FORBIDDEN_CONSTRUCT", "NATIVE_FORMAT_ENCODING")
)


@dataclass(frozen=True, slots=True)
class _ManifestCase:
    identifier: str
    positive: bool
    action: Path
    result: Path | None


def _manifest_cases() -> tuple[_ManifestCase, ...]:
    text = MANIFEST.read_text()
    entries_match = re.search(r"(?ms)\bmf:entries\s*\((.*?)^\s*\)\s*\.", text)
    if entries_match is None:
        raise AssertionError("W3C RDF/XML manifest has no entry ledger")
    identifiers = tuple(
        match.group(1)
        for line in entries_match.group(1).splitlines()
        if not line.lstrip().startswith("#")
        if (match := re.search(r"<#([^>]+)>", line)) is not None
    )
    blocks = {
        match.group(1): match.group(2)
        for match in re.finditer(r"(?ms)^<#([^>]+)>(.*?)(?=^<#|\Z)", text)
    }
    cases: list[_ManifestCase] = []
    for identifier in identifiers:
        block = blocks.get(identifier)
        if block is None:
            raise AssertionError(f"W3C RDF/XML manifest entry {identifier!r} has no definition")
        kind = re.search(r"\ba rdft:(TestXMLEval|TestXMLNegativeSyntax)\s*;", block)
        action = re.search(r"\bmf:action <([^>]+\.rdf)>", block)
        result = re.search(r"\bmf:result <([^>]+\.nt)>", block)
        if kind is None or action is None:
            raise AssertionError(f"W3C RDF/XML manifest entry {identifier!r} is incomplete")
        positive = kind.group(1) == "TestXMLEval"
        if positive != (result is not None):
            raise AssertionError(f"W3C RDF/XML manifest entry {identifier!r} has invalid result")
        cases.append(
            _ManifestCase(
                identifier,
                positive,
                CORPUS / action.group(1),
                None if result is None else CORPUS / result.group(1),
            )
        )
    return tuple(cases)


CASES = _manifest_cases()


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    if not hasattr(selected, "_parse_rdfxml_graph_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail("selected native test-hooks artifact lacks the RDF/XML graph observer")
        pytest.skip("selected native artifact lacks the W3C RDF/XML graph test hook")
    return selected


def _python_graph(source: bytes, document_iri: str) -> RDFGraph:
    text, source_encoding = _decode_xml_source(source)
    _validate_xml_envelope(text, source_encoding)
    if _has_forbidden_xml_markup(text):
        raise OntologySyntaxError(
            "DTD and entity declarations are forbidden",
            code="XML_FORBIDDEN_CONSTRUCT",
        )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise OntologySyntaxError("malformed RDF/XML document", code="RDFXML_SYNTAX") from error
    limits = ParseLimits()
    context = ParseContext(limits, None)
    lexical_count, reserved_by_ordinal = _reserved_xml_attribute_names(text, context)
    elements = tuple(root.iter())
    if len(elements) != lexical_count:
        raise AssertionError("RDF/XML lexical and structural element streams diverged")
    reserved = {
        id(element): values
        for ordinal, element in enumerate(elements)
        if (values := reserved_by_ordinal.get(ordinal))
    }
    return RDFXMLGraphParser(root, limits, IRI(document_iri), None, reserved).parse()


def _native_graph(
    extension: NativeTestExtension,
    source: bytes,
    document_iri: str,
) -> RDFGraph:
    config = cast(Any, native)._encode_config(ParseLimits(), None, verify=False)
    encoded = cast(Any, extension)._parse_rdfxml_graph_v1(
        source,
        document_iri,
        config,
        None,
    )
    return _decode_graph_observation(encoded)


def _decode_graph_observation(data: bytes) -> RDFGraph:
    if not isinstance(data, bytes) or len(data) < 20:
        raise AssertionError("truncated native RDF/XML graph observation")
    magic, schema, flags, count = struct.unpack_from("<8sHHQ", data)
    if (magic, schema, flags) != (b"PYRXGRF1", 1, 0):
        raise AssertionError("invalid native RDF/XML graph observation header")
    offset = 20

    def byte() -> int:
        nonlocal offset
        if offset >= len(data):
            raise AssertionError("truncated native RDF/XML graph observation byte")
        value = data[offset]
        offset += 1
        return value

    def frame() -> str:
        nonlocal offset
        if offset + 8 > len(data):
            raise AssertionError("truncated native RDF/XML graph observation frame")
        (size,) = struct.unpack_from("<Q", data, offset)
        offset += 8
        end = offset + size
        if end > len(data):
            raise AssertionError("truncated native RDF/XML graph observation payload")
        try:
            value = data[offset:end].decode()
        except UnicodeDecodeError as error:
            raise AssertionError("invalid native RDF/XML graph observation UTF-8") from error
        offset = end
        return value

    def optional() -> str | None:
        marker = byte()
        if marker == 0:
            return None
        if marker != 1:
            raise AssertionError("invalid native RDF/XML graph optional marker")
        return frame()

    def resource() -> RDFResource:
        tag = byte()
        value = frame()
        if tag == 0:
            return RDFIRI(value)
        if tag == 1:
            return RDFBlank(value)
        raise AssertionError("invalid native RDF/XML graph resource tag")

    def term() -> RDFTerm:
        tag = byte()
        if tag == 0:
            return RDFIRI(frame())
        if tag == 1:
            return RDFBlank(frame())
        if tag == 2:
            return RDFLiteral(frame(), optional(), optional())
        raise AssertionError("invalid native RDF/XML graph term tag")

    triples = tuple(Triple(resource(), RDFIRI(frame()), term()) for _ in range(count))
    if offset != len(data):
        raise AssertionError("trailing native RDF/XML graph observation bytes")
    return RDFGraph(triples)


def _expected_graph(path: Path) -> RDFGraph:
    graph, prefixes = TurtleParser(path.read_text(), ParseLimits(), None, None).parse()
    if prefixes:
        raise AssertionError("N-Triples fixture unexpectedly declared prefixes")
    return graph


def _isomorphic(left: RDFGraph, right: RDFGraph) -> bool:
    left_blanks = sorted(
        {
            value.label
            for triple in left.triples
            for value in (triple.subject, triple.object)
            if isinstance(value, RDFBlank)
        }
    )
    right_blanks = sorted(
        {
            value.label
            for triple in right.triples
            for value in (triple.subject, triple.object)
            if isinstance(value, RDFBlank)
        }
    )
    if len(left.triples) != len(right.triples) or len(left_blanks) != len(right_blanks):
        return False
    expected = frozenset(right.triples)
    for candidate in itertools.permutations(right_blanks):
        mapping = dict(zip(left_blanks, candidate, strict=True))
        if frozenset(_map_triple(triple, mapping) for triple in left.triples) == expected:
            return True
    return False


def _map_triple(triple: Triple, mapping: dict[str, str]) -> Triple:
    subject = (
        RDFBlank(mapping[triple.subject.label])
        if isinstance(triple.subject, RDFBlank)
        else triple.subject
    )
    object_value = (
        RDFBlank(mapping[triple.object.label])
        if isinstance(triple.object, RDFBlank)
        else triple.object
    )
    return Triple(subject, triple.predicate, object_value)


def test_locked_w3c_rdfxml_corpus_matches_upstream_identity() -> None:
    lock = tomllib.loads(UPSTREAM.read_text())
    files = tuple(
        sorted(
            (path for path in CORPUS.rglob("*") if path.is_file() and path != UPSTREAM),
            key=lambda path: path.relative_to(CORPUS).as_posix(),
        )
    )
    digest = hashlib.sha256()
    digest.update(_AGGREGATE_DOMAIN)
    for path in files:
        name = path.relative_to(CORPUS).as_posix().encode()
        payload = path.read_bytes()
        digest.update(struct.pack("<Q", len(name)))
        digest.update(name)
        digest.update(struct.pack("<Q", len(payload)))
        digest.update(payload)

    assert lock == {
        "schema": 1,
        "repository": "https://github.com/w3c/rdf-tests.git",
        "revision": "3d0b0613d0177d25aad7ec60e88df2338f461516",
        "upstream_path": "rdf/rdf11/rdf-xml",
        "upstream_tree": "92e606ab2efa865ba89510bef6650a5fd03f2728",
        "file_count": 317,
        "aggregate_domain": "pyowl-core:w3c-rdfxml-corpus:v1",
        "aggregate_sha256": "d10676e0219e5dd174d3f5f0451dee929549d056a7e9622b4b978af34e335ed0",
        "license_expression": "W3C-Test-Suite OR BSD-3-Clause",
        "selected_license": "BSD-3-Clause",
        "license_file": "THIRD_PARTY_LICENSES/W3C-RDF-tests-BSD-3-Clause.txt",
    }
    assert len(files) == lock["file_count"]
    assert digest.hexdigest() == lock["aggregate_sha256"]
    assert len(CASES) == 166
    assert sum(case.positive for case in CASES) == 126


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
def test_w3c_rdfxml_manifest_case(
    extension: NativeTestExtension,
    case: _ManifestCase,
) -> None:
    source = case.action.read_bytes()
    document_iri = ASSUMED_BASE + case.action.relative_to(CORPUS).as_posix()
    if not case.positive:
        with pytest.raises(OntologySyntaxError):
            _python_graph(source, document_iri)
        with pytest.raises(extension._NativeError) as native_error:
            _native_graph(extension, source, document_iri)
        assert native_error.value.args[0] in _NATIVE_SYNTAX_ERRORS
        return

    if case.result is None:
        raise AssertionError("positive W3C RDF/XML case has no result")
    expected = _expected_graph(case.result)
    python = _python_graph(source, document_iri)
    selected = _native_graph(extension, source, document_iri)

    assert _isomorphic(python, expected)
    assert _isomorphic(selected, expected)
    assert _isomorphic(selected, python)
