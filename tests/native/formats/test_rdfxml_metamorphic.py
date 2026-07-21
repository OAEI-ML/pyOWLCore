from __future__ import annotations

from typing import Any, cast

import pytest

from pyowl_core import ParseLimits, canonical_bytes
from pyowl_core.io.formats.rdfxml import parse_rdfxml
from tests.native.formats.test_rdfxml_ingestion_slice import _ingest
from tests.native.foundation._support import NativeTestExtension, load_extension

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"

DIRECT_CLASS = f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:owl="{OWL}">
  <owl:Class rdf:about="urn:C"/>
</rdf:RDF>
""".encode()

DESCRIPTION_CLASS = f"""\
<r:RDF xmlns:r="{RDF}" xmlns:o="{OWL}">
  <!-- the prefix and node-element spelling are nonstructural -->
  <r:Description r:about="urn:C">
    <r:type r:resource="{OWL}Class"/>
  </r:Description>
</r:RDF>
""".encode()

ROOT_CLASS = f"""\
<owl:Class xmlns:rdf="{RDF}" xmlns:owl="{OWL}" rdf:about="urn:C"/>
""".encode()

DUPLICATE_CLASS = f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:owl="{OWL}">
  <owl:Class rdf:about="urn:C"/>
  <owl:Class rdf:about="urn:C"/>
</rdf:RDF>
""".encode()

ORDERED_SUBCLASS = f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:rdfs="{RDFS}" xmlns:owl="{OWL}">
  <owl:Class rdf:about="urn:A">
    <rdfs:subClassOf rdf:resource="urn:B"/>
  </owl:Class>
  <owl:Class rdf:about="urn:B"/>
</rdf:RDF>
""".encode()

REVERSED_SUBCLASS = f"""\
<rdf:RDF xmlns:owl="{OWL}" xmlns:rdfs="{RDFS}" xmlns:rdf="{RDF}">
  <owl:Class rdf:about="urn:B"/>
  <!-- graph statement order does not affect the structural result -->
  <owl:Class rdf:about="urn:A">
    <rdfs:subClassOf rdf:resource="urn:B"/>
  </owl:Class>
</rdf:RDF>
""".encode()

DUPLICATE_INTERSECTION = f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:rdfs="{RDFS}" xmlns:owl="{OWL}">
  <owl:Class rdf:about="urn:A">
    <rdfs:subClassOf>
      <owl:Class>
        <owl:intersectionOf rdf:parseType="Collection">
          <owl:Class rdf:about="urn:B"/>
          <owl:Class rdf:about="urn:B"/>
        </owl:intersectionOf>
      </owl:Class>
    </rdfs:subClassOf>
  </owl:Class>
</rdf:RDF>
""".encode()

SELF_DISJOINT = f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:owl="{OWL}">
  <owl:Class rdf:about="urn:C">
    <owl:disjointWith rdf:resource="urn:C"/>
  </owl:Class>
</rdf:RDF>
""".encode()

BOTTOM_SUBCLASS = f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:rdfs="{RDFS}" xmlns:owl="{OWL}">
  <owl:Class rdf:about="urn:C">
    <rdfs:subClassOf rdf:resource="{OWL}Nothing"/>
  </owl:Class>
</rdf:RDF>
""".encode()

DUPLICATE_ALL_DISJOINT = f"""\
<rdf:RDF xmlns:rdf="{RDF}" xmlns:owl="{OWL}">
  <owl:AllDisjointClasses>
    <owl:members rdf:parseType="Collection">
      <owl:Class rdf:about="urn:C"/>
      <owl:Class rdf:about="urn:C"/>
    </owl:members>
  </owl:AllDisjointClasses>
</rdf:RDF>
""".encode()


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    if not hasattr(selected, "_ingest_rdfxml_slice_v1"):
        pytest.skip("selected native artifact lacks the WP16 RDF/XML test hook")
    return selected


@pytest.mark.parametrize(
    ("first", "second"),
    (
        (DIRECT_CLASS, DESCRIPTION_CLASS),
        (DIRECT_CLASS, ROOT_CLASS),
        (DIRECT_CLASS, DUPLICATE_CLASS),
        (ORDERED_SUBCLASS, REVERSED_SUBCLASS),
        (ORDERED_SUBCLASS, DUPLICATE_INTERSECTION),
        (SELF_DISJOINT, BOTTOM_SUBCLASS),
        (SELF_DISJOINT, DUPLICATE_ALL_DISJOINT),
    ),
    ids=(
        "description-type-and-prefix",
        "optional-rdf-wrapper",
        "duplicate-triples",
        "statement-order",
        "idempotent-boolean",
        "self-disjoint-bottom",
        "duplicate-all-disjoint",
    ),
)
def test_equivalent_rdfxml_graphs_have_exact_python_native_roots(
    extension: NativeTestExtension,
    first: bytes,
    second: bytes,
) -> None:
    first_python, first_native = _mapped(extension, first)
    second_python, second_native = _mapped(extension, second)

    assert first_python.ontology_id == second_python.ontology_id
    assert first_python.imports == second_python.imports
    assert first_python.annotations == second_python.annotations
    assert first_python.axioms == second_python.axioms
    assert first_python.extensions == second_python.extensions
    assert first_native.axioms == second_native.axioms


def _mapped(extension: NativeTestExtension, source: bytes) -> tuple[Any, Any]:
    python = parse_rdfxml(source, limits=ParseLimits(), document_iri=None)
    owner, native = _ingest(extension, source)
    assert python.rdf_mapping_report is not None
    assert native.ontology_iri == (
        None
        if python.ontology_id.ontology_iri is None
        else python.ontology_id.ontology_iri.value
    )
    assert native.version_iri == (
        None if python.ontology_id.version_iri is None else python.ontology_id.version_iri.value
    )
    assert native.imports == tuple(value.value for value in python.imports)
    assert native.axioms == tuple(sorted(canonical_bytes(value) for value in python.axioms))
    assert native.total_triples == native.consumed_triples
    assert native.total_triples == python.rdf_mapping_report.total_triples
    assert native.decoded_codepoints == python.decoded_codepoint_length
    attestation = cast(Any, owner)._publication_attestation_v1()
    assert attestation.ontology_annotation_count == len(python.annotations)
    assert attestation.stored_axiom_count == len(python.axioms)
    return python, native
