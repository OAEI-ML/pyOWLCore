from __future__ import annotations

from unittest.mock import patch

import pytest

import pyowl_core.model as m
from pyowl_core import (
    BackendPreference,
    BackendUnavailableError,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologySyntaxError,
    OptionConflictError,
    ParseLimits,
    PythonParser,
    ResourceLimitError,
    UnsupportedSyntaxError,
    load_snapshot,
    parse_document,
    render_document,
)
from pyowl_core.backends.native import NativeProbe

PYTHON_OPTIONS = LoadOptions(backend=BackendPreference.PYTHON)
ONTOLOGY = "https://example.org/w3c-derived"
CLASS = ONTOLOGY + "#C"
RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

FUNCTIONAL = f"Ontology(<{ONTOLOGY}> Declaration(Class(<{CLASS}>)))".encode()
OWL_XML = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="{ONTOLOGY}">
  <Declaration><Class IRI="{CLASS}"/></Declaration>
</Ontology>
""".encode()
TURTLE = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY}> a owl:Ontology .
<{CLASS}> a owl:Class .
""".encode()
RDF_XML = f"""\
<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="{ONTOLOGY}"/>
  <owl:Class rdf:about="{CLASS}"/>
</rdf:RDF>
""".encode()


@pytest.mark.parametrize(
    ("format", "source"),
    [
        (DocumentFormat.FUNCTIONAL, FUNCTIONAL),
        (DocumentFormat.OWL_XML, OWL_XML),
        (DocumentFormat.TURTLE, TURTLE),
        (DocumentFormat.RDF_XML, RDF_XML),
    ],
)
def test_w3c_derived_minimal_documents_have_one_structure(
    format: DocumentFormat, source: bytes
) -> None:
    document = parse_document(
        source,
        format=format,
        document_iri=ONTOLOGY,
        options=PYTHON_OPTIONS,
    )
    assert document.ontology_id.ontology_iri == m.IRI(ONTOLOGY)
    assert document.axioms == m.CanonicalSet((m.Declaration(m.Class(m.IRI(CLASS))),))


def test_rdfxml_parse_type_other_has_xml_literal_semantics() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:x="urn:parse-other:">
  <owl:Class rdf:about="{CLASS}">
    <rdfs:comment rdf:parseType="Other">root<x:value>text</x:value>tail</rdfs:comment>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assertion = next(document.iter_axioms(m.AnnotationAssertion))
    assert isinstance(assertion.value, m.Literal)
    assert assertion.value.lexical_form == (
        'root<ns0:value xmlns:ns0="urn:parse-other:">text</ns0:value>tail'
    )
    assert assertion.value.datatype.iri.value == RDF_NAMESPACE + "XMLLiteral"
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.total_triples == 2
    assert document.rdf_mapping_report.consumed_triples == 2


def test_rdfxml_empty_language_resets_inherited_literal_language() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xml:lang="EN">
  <owl:Class rdf:about="{CLASS}" xml:lang="" rdfs:label="attribute">
    <rdfs:comment>element</rdfs:comment>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assertions = tuple(document.iter_axioms(m.AnnotationAssertion))

    assert {value.value.lexical_form for value in assertions} == {"attribute", "element"}
    assert all(value.value.language is None for value in assertions)
    assert all(value.value.datatype == m.XSD_STRING for value in assertions)


@pytest.mark.parametrize(
    "document",
    (
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:label xml:lang='not_valid'>value</rdfs:label>"
            "</owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C' "
            "xml:lang='en--GB' rdfs:label='value'/></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces} xml:lang='x'><owl:Class rdf:about='urn:C'>"
            "<rdfs:label>value</rdfs:label></owl:Class></rdf:RDF>"
        ),
    ),
)
def test_rdfxml_invalid_language_tags_fail_at_mapping_boundary(document: str) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_UNSUPPORTED"


def test_rdfxml_unicode_ids_are_unique_within_each_xml_base() -> None:
    source = """\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xml:base="https://example.org/a/doc">
  <owl:Class rdf:ID="classe-é"/>
  <owl:Class xml:base="../b/doc" rdf:ID="classe-é"/>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)

    assert document.axioms == m.CanonicalSet(
        (
            m.Declaration(m.Class(m.IRI("https://example.org/a/doc#classe-é"))),
            m.Declaration(m.Class(m.IRI("https://example.org/b/doc#classe-é"))),
        )
    )


@pytest.mark.parametrize(
    ("attribute", "value"),
    (
        ("ID", ""),
        ("ID", "1leading-digit"),
        ("ID", "bad:name"),
        ("nodeID", ""),
        ("nodeID", "1leading-digit"),
        ("nodeID", "bad:name"),
    ),
)
def test_rdfxml_identity_attributes_require_xml_ncnames(attribute: str, value: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xml:base="https://example.org/doc">
  <owl:Class rdf:{attribute}="{value}"/>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


def test_rdfxml_duplicate_id_within_one_xml_base_is_rejected() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xml:base="https://example.org/doc">
  <owl:Class rdf:ID="duplicate"/>
  <owl:Class rdf:ID="duplicate"/>
</rdf:RDF>
"""

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize("declared_encoding", ("UTF-8", "UTF8", "US-ASCII"))
def test_rdfxml_utf8_declaration_aliases_use_decoded_text(declared_encoding: str) -> None:
    source = f"""\
<?xml version='1.0' encoding='{declared_encoding}'?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="{CLASS}"><rdfs:label>café</rdfs:label></owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assertion = next(document.iter_axioms(m.AnnotationAssertion))

    assert isinstance(assertion.value, m.Literal)
    assert assertion.value.lexical_form == "café"


@pytest.mark.parametrize(
    ("declaration", "code"),
    (
        ("<?xml encoding='UTF-8'?>", "RDFXML_SYNTAX"),
        ("<?xml version=''?>", "RDFXML_SYNTAX"),
        ("<?xml version='1.1'?>", "RDFXML_SYNTAX"),
        ("<?xml version='2.0'?>", "RDFXML_SYNTAX"),
        ("<?xml version='1.0' unknown='value'?>", "RDFXML_SYNTAX"),
        ("<?xml version='1.0' standalone='true'?>", "RDFXML_SYNTAX"),
        (
            "<?xml version='1.0' standalone='yes' encoding='UTF-8'?>",
            "RDFXML_SYNTAX",
        ),
        (
            "<?xml version='1.0' encoding='ISO-8859-1'?>",
            "XML_FORBIDDEN_CONSTRUCT",
        ),
    ),
)
def test_rdfxml_xml_declaration_contract_is_strict(declaration: str, code: str) -> None:
    source = (
        declaration
        + '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == code


@pytest.mark.parametrize(
    "source",
    (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'.encode(
            "utf-32"
        ),
        b"\xff\xfe\x00\xd8",
        b"\xff\xfe<\x00x",
    ),
)
def test_rdfxml_invalid_or_unsupported_source_encoding_is_rejected(source: bytes) -> None:
    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "FORMAT_ENCODING"


@pytest.mark.parametrize(
    "class_element",
    (
        "<owl:Class rdf:about='urn:C' rdfs:label='&external;'/>",
        "<owl:Class rdf:about='urn:C' rdfs:label='&entité;'/>",
        (
            "<owl:Class rdf:about='urn:C'>"
            "<rdfs:label>&external;</rdfs:label>"
            "</owl:Class>"
        ),
    ),
)
def test_rdfxml_unknown_entity_references_are_forbidden(class_element: str) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'>"
        f"{class_element}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "XML_FORBIDDEN_CONSTRUCT"


@pytest.mark.parametrize(
    "class_element",
    (
        "<owl:Class rdf:about='urn:C' rdfs:label='&external'/>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&amp</rdfs:label></owl:Class>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&1bad;</rdfs:label></owl:Class>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&bad name;</rdfs:label></owl:Class>",
        "<owl:Class rdf:about='urn:C'><rdfs:label>&;</rdfs:label></owl:Class>",
    ),
)
def test_rdfxml_malformed_entity_references_are_syntax_errors(class_element: str) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'>"
        f"{class_element}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


def test_rdfxml_forbidden_keywords_are_inert_inside_xml_data_regions() -> None:
    source = b"""\
<?audit <!DOCTYPE inert>?>
<!-- <!ENTITY inert> -->
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <rdfs:label><![CDATA[<!DOCTYPE inert>]]></rdfs:label>
  </owl:Class>
</rdf:RDF>
"""

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assertion = next(document.iter_axioms(m.AnnotationAssertion))

    assert isinstance(assertion.value, m.Literal)
    assert assertion.value.lexical_form == "<!DOCTYPE inert>"


@pytest.mark.parametrize(
    "instruction",
    ("<??>", "<?1target?>", "<?target/data?>", "<?a:b?>", "<?XML version='1.0'?>"),
)
def test_rdfxml_malformed_processing_instructions_are_syntax_errors(
    instruction: str,
) -> None:
    source = (
        f"{instruction}<rdf:RDF "
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "document",
    (
        (
            "<rdf:RDF {namespaces}>text"
            "<owl:Class rdf:about='urn:C'/>"
            "</rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C'/>tail"
            "</rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C'/>between"
            "<owl:Class rdf:about='urn:D'/>"
            "</rdf:RDF>"
        ),
        "<owl:Class {namespaces} rdf:about='urn:C'>text</owl:Class>",
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C'>"
            "<rdfs:label>value</rdfs:label>tail"
            "</owl:Class>"
            "</rdf:RDF>"
        ),
    ),
)
def test_rdfxml_root_and_node_elements_reject_character_data(document: str) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "document",
    (
        "<rdf:RDF {namespaces} e:ignored='value'/>",
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:subClassOf rdf:parseType='Resource' e:ignored='value'/>"
            "</owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<owl:equivalentClass rdf:parseType='Collection' e:ignored='value'/>"
            "</owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:label rdf:parseType='Literal' e:ignored='value'>text</rdfs:label>"
            "</owl:Class></rdf:RDF>"
        ),
    ),
)
def test_rdfxml_root_and_parse_type_attributes_are_restricted(document: str) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:e='urn:e:'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize(
    ("role", "local"),
    [
        *[
            ("node", local)
            for local in (
                "RDF",
                "ID",
                "about",
                "parseType",
                "resource",
                "nodeID",
                "datatype",
                "li",
                "aboutEach",
                "aboutEachPrefix",
                "bagID",
            )
        ],
        *[
            ("property", local)
            for local in (
                "RDF",
                "ID",
                "about",
                "parseType",
                "resource",
                "nodeID",
                "datatype",
                "Description",
                "aboutEach",
                "aboutEachPrefix",
                "bagID",
            )
        ],
    ],
)
def test_rdfxml_reserved_names_are_rejected_in_element_roles(
    role: str,
    local: str,
) -> None:
    content = (
        f"<rdf:{local}/>"
        if role == "node"
        else f"<rdf:Description rdf:about='urn:s'><rdf:{local}/></rdf:Description>"
    )
    source = (
        f"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>{content}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "document",
    (
        "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'><label/></owl:Class></rdf:RDF>",
        "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C' label='value'/></rdf:RDF>",
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:label note='value'/></owl:Class></rdf:RDF>"
        ),
        (
            "<rdf:RDF {namespaces} xmlns='urn:default:'>"
            "<owl:Class rdf:about='urn:C' label='value'/></rdf:RDF>"
        ),
        "<rdf:RDF {namespaces}><owl:1Class/></rdf:RDF>",
        "<rdf:RDF {namespaces}><owl:Class owl:1label='value'/></rdf:RDF>",
        "<rdf:RDF {namespaces} xmlns:1bad='urn:bad:'/>",
    ),
)
def test_rdfxml_namespace_and_qname_errors_are_syntax_errors(document: str) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#'"
    )
    source = document.format(namespaces=namespaces).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


def test_rdfxml_namespace_declarations_enforce_the_prefix_limit() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:e="urn:first:">
  <owl:Class xmlns:e="urn:second:" rdf:about="urn:C"/>
</rdf:RDF>
"""

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_prefixes=4),
        ),
    )
    assert document.axioms == m.CanonicalSet((m.Declaration(m.Class(m.IRI("urn:C"))),))

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_prefixes=3),
            ),
        )
    assert raised.value.limit == "max_prefixes"


@pytest.mark.parametrize(
    ("document", "expanded_iri_bytes"),
    (
        ("<rdf:RDF {namespace}/>", 46),
        ("<rdf:Description {namespace} rdf:about='urn:C'/>", 54),
    ),
)
def test_rdfxml_reserved_element_iris_enforce_utf8_byte_limits(
    document: str,
    expanded_iri_bytes: int,
) -> None:
    source = document.format(
        namespace="xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'"
    ).encode()

    parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_iri_bytes=expanded_iri_bytes),
        ),
    )
    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_iri_bytes=expanded_iri_bytes - 1),
            ),
        )
    assert raised.value.limit == "max_iri_bytes"


@pytest.mark.parametrize(
    ("document", "literal_bytes"),
    (
        (
            "<rdf:RDF {namespaces}>"
            "<owl:Class rdf:about='urn:C' rdfs:label='é'/>"
            "</rdf:RDF>",
            2,
        ),
        (
            "<rdf:RDF {namespaces}>"
            "<owl:AnnotationProperty rdf:about='urn:e:p'/>"
            "<owl:Class rdf:about='urn:C'>"
            "<e:p rdf:resource='urn:o' rdfs:label='é'/>"
            "</owl:Class></rdf:RDF>",
            2,
        ),
        (
            "<rdf:RDF {namespaces}><owl:Class rdf:about='urn:C'>"
            "<rdfs:comment rdf:parseType='Literal'><e:x/></rdfs:comment>"
            "</owl:Class></rdf:RDF>",
            28,
        ),
    ),
)
def test_rdfxml_all_literal_forms_enforce_utf8_byte_limits(
    document: str,
    literal_bytes: int,
) -> None:
    namespaces = (
        "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:e='urn:e:'"
    )
    source = document.format(namespaces=namespaces).encode()

    parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_literal_bytes=literal_bytes),
        ),
    )
    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_literal_bytes=literal_bytes - 1),
            ),
        )
    assert raised.value.limit == "max_literal_bytes"


@pytest.mark.parametrize(
    "property_element",
    (
        "<rdfs:subClassOf rdf:resource='urn:D' rdf:datatype='urn:type'/>",
        "<rdfs:subClassOf rdf:nodeID='target' rdf:datatype='urn:type'/>",
        "<rdfs:subClassOf rdf:resource='urn:D'>text</rdfs:subClassOf>",
        "<rdfs:subClassOf>text<owl:Class rdf:about='urn:D'/></rdfs:subClassOf>",
        "<rdfs:subClassOf><owl:Class rdf:about='urn:D'/>text</rdfs:subClassOf>",
        "<rdfs:label e:note='ignored'>text</rdfs:label>",
        (
            "<rdfs:subClassOf e:note='ignored'>"
            "<owl:Class rdf:about='urn:D'/>"
            "</rdfs:subClassOf>"
        ),
        (
            "<owl:equivalentClass><owl:Class>"
            "<owl:unionOf rdf:parseType='Collection'>text"
            "<rdf:Description rdf:about='urn:D'/>"
            "</owl:unionOf></owl:Class></owl:equivalentClass>"
        ),
        (
            "<rdfs:subClassOf rdf:parseType='Resource'>text"
            "<owl:onProperty rdf:resource='urn:p'/>"
            "<owl:someValuesFrom rdf:resource='urn:D'/>"
            "</rdfs:subClassOf>"
        ),
    ),
)
def test_rdfxml_resource_property_grammar_rejects_conflicts_and_text(
    property_element: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:e="urn:e:">
  <owl:Class rdf:about="urn:C">{property_element}</owl:Class>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


def test_rdfxml_resource_properties_allow_inter_element_whitespace() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <rdfs:subClassOf rdf:resource="urn:D"> \n </rdfs:subClassOf>
    <owl:equivalentClass> \n <owl:Class rdf:about="urn:E"/> \n </owl:equivalentClass>
  </owl:Class>
</rdf:RDF>
"""

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)

    assert document.axioms == m.CanonicalSet(
        (
            m.Declaration(m.Class(m.IRI("urn:C"))),
            m.Declaration(m.Class(m.IRI("urn:E"))),
            m.SubClassOf(m.Class(m.IRI("urn:C")), m.Class(m.IRI("urn:D"))),
            m.EquivalentClasses(
                m.CanonicalSet((m.Class(m.IRI("urn:C")), m.Class(m.IRI("urn:E"))))
            ),
        )
    )


def test_rdfxml_rfc3986_resolution_normalizes_absolute_and_relative_paths() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="http://example.test/a/../C"/>
  <owl:Class rdf:about="../D"/>
</rdf:RDF>
"""

    document = parse_document(
        source,
        format="rdfxml",
        document_iri="http://example.test/root/doc.owl",
        options=PYTHON_OPTIONS,
    )

    assert document.axioms == m.CanonicalSet(
        (
            m.Declaration(m.Class(m.IRI("http://example.test/C"))),
            m.Declaration(m.Class(m.IRI("http://example.test/D"))),
        )
    )


@pytest.mark.parametrize(
    ("reference", "document_iri", "code"),
    (
        ("1:invalid", "http://example.test/doc", "RDFXML_IRI_REFERENCE"),
        (" .", "http://example.test/doc", "RDFXML_SYNTAX"),
        ("g h", "http://example.test/doc", "RDFXML_SYNTAX"),
        ("%zz", "http://example.test/doc", "RDFXML_SYNTAX"),
        ("relative", None, "RDFXML_RELATIVE_IRI_NO_BASE"),
    ),
)
def test_rdfxml_invalid_iri_references_fail_as_syntax(
    reference: str,
    document_iri: str | None,
    code: str,
) -> None:
    source = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:owl="http://www.w3.org/2002/07/owl#">'
        f'<owl:Class rdf:about="{reference}"/></rdf:RDF>'
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(
            source,
            format="rdfxml",
            document_iri=document_iri,
            options=PYTHON_OPTIONS,
        )
    assert raised.value.code == code


@pytest.mark.parametrize(
    "node",
    (
        "<e:Class rdf:about='urn:C'/>",
        "<owl:Class rdf:about='urn:C' e:label='value'/>",
    ),
)
def test_rdfxml_expanded_names_require_absolute_iris(node: str) -> None:
    source = (
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xmlns:e='relative'>"
        f"{node}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


def test_disabled_provenance_omits_document_and_snapshot_origins() -> None:
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
        collect_provenance=False,
    )
    document = parse_document(FUNCTIONAL, options=options)
    snapshot = load_snapshot(FUNCTIONAL, options=options)

    assert document.origin_index is None
    assert not snapshot.origin_index.entries


@pytest.mark.parametrize("format", tuple(DocumentFormat))
def test_each_writer_is_deterministic_and_round_trips(format: DocumentFormat) -> None:
    document = parse_document(FUNCTIONAL, format="functional", options=PYTHON_OPTIONS)
    first = render_document(document, format=format)
    second = render_document(document, format=format)
    assert first == second
    reparsed = parse_document(
        first,
        format=format,
        document_iri=ONTOLOGY,
        options=PYTHON_OPTIONS,
    )
    assert reparsed == document
    assert reparsed.document_fingerprint == document.document_fingerprint


def test_blank_node_labels_are_document_local_and_alpha_invariant() -> None:
    first = b"Ontology(ClassAssertion(ObjectOneOf(_:left) _:left))"
    second = b"Ontology(ClassAssertion(ObjectOneOf(_:renamed) _:renamed))"
    first_document = parse_document(first, format="functional", options=PYTHON_OPTIONS)
    second_document = parse_document(second, format="functional", options=PYTHON_OPTIONS)
    assert first_document == second_document
    assert first_document.document_fingerprint == second_document.document_fingerprint


def test_plain_literal_at_sign_is_not_reinterpreted_as_a_language_tag() -> None:
    source = b'Ontology(AnnotationAssertion(<https://e/p> <https://e/s> "email@example"))'
    document = parse_document(source, format="functional", options=PYTHON_OPTIONS)
    assertion = next(document.iter_axioms(m.AnnotationAssertion))
    assert isinstance(assertion.value, m.Literal)
    assert assertion.value.lexical_form == "email@example"
    assert assertion.value.language is None


def test_prefixed_has_key_class_is_not_mistaken_for_a_constructor() -> None:
    source = b"Prefix(:=<urn:key#>) Ontology(HasKey(:A (:r) ()))"
    document = parse_document(source, format="functional", options=PYTHON_OPTIONS)
    assert document.axioms == m.CanonicalSet(
        (
            m.HasKey(
                m.Class(m.IRI("urn:key#A")),
                m.CanonicalSet((m.ObjectProperty(m.IRI("urn:key#r")),)),
                m.CanonicalSet(),
            ),
        )
    )


def test_partial_rdf_mapping_is_expert_only_and_reported() -> None:
    source = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY}> a owl:Ontology .
<{ONTOLOGY}#s> <{ONTOLOGY}#unknown> <{ONTOLOGY}#o> .
""".encode()
    with pytest.raises(UnsupportedSyntaxError, match="not completely mappable"):
        parse_document(
            source,
            format="turtle",
            document_iri=ONTOLOGY,
            options=PYTHON_OPTIONS,
        )
    partial = PythonParser().parse(
        source,
        format="turtle",
        document_iri=ONTOLOGY,
        options=PYTHON_OPTIONS,
        allow_partial_rdf_mapping=True,
    )
    assert partial.rdf_mapping_report is not None
    assert not partial.rdf_mapping_report.conformant
    assert partial.rdf_mapping_report.total_triples == 2
    assert partial.rdf_mapping_report.consumed_triples == 1
    assert len(partial.rdf_mapping_report.unconsumed) == 1


def test_backend_and_format_option_conflicts_are_explicit() -> None:
    with (
        patch(
            "pyowl_core.backends.native.probe",
            return_value=NativeProbe(False, "test backend unavailable", None, ()),
        ),
        pytest.raises(BackendUnavailableError),
    ):
        parse_document(
            FUNCTIONAL,
            format="functional",
            options=LoadOptions(backend=BackendPreference.NATIVE),
        )
    with pytest.raises(OptionConflictError):
        parse_document(
            FUNCTIONAL,
            format="functional",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                format=DocumentFormat.TURTLE,
            ),
        )
