from __future__ import annotations

from unittest.mock import patch

import pytest

import pyowl_core.extensions.swrl as swrl
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
XSD_NAMESPACE = "http://www.w3.org/2001/XMLSchema#"
RDFS_NAMESPACE = "http://www.w3.org/2000/01/rdf-schema#"
OWL_NAMESPACE = "http://www.w3.org/2002/07/owl#"
OWL2_BUILTIN_DATATYPES = (
    RDFS_NAMESPACE + "Literal",
    RDF_NAMESPACE + "PlainLiteral",
    RDF_NAMESPACE + "XMLLiteral",
    OWL_NAMESPACE + "real",
    OWL_NAMESPACE + "rational",
    *(
        XSD_NAMESPACE + local
        for local in (
            "anyURI",
            "base64Binary",
            "boolean",
            "byte",
            "dateTime",
            "dateTimeStamp",
            "decimal",
            "double",
            "float",
            "hexBinary",
            "int",
            "integer",
            "language",
            "long",
            "Name",
            "NCName",
            "negativeInteger",
            "NMTOKEN",
            "nonNegativeInteger",
            "nonPositiveInteger",
            "normalizedString",
            "positiveInteger",
            "short",
            "string",
            "token",
            "unsignedByte",
            "unsignedInt",
            "unsignedLong",
            "unsignedShort",
        )
    ),
)

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
SWRL_RDF_XML = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:swrl="http://www.w3.org/2003/11/swrl#"
         xmlns:e="urn:">
  <swrl:Variable rdf:about="urn:x"/>
  <swrl:Variable rdf:about="urn:y"/>
  <swrl:Imp rdf:nodeID="rule">
    <swrl:body rdf:parseType="Collection">
      <swrl:ClassAtom>
        <swrl:classPredicate rdf:resource="urn:C"/>
        <swrl:argument1 rdf:resource="urn:x"/>
      </swrl:ClassAtom>
      <swrl:DataRangeAtom>
        <swrl:dataRange rdf:resource="urn:D"/>
        <swrl:argument1 rdf:resource="urn:y"/>
      </swrl:DataRangeAtom>
      <swrl:IndividualPropertyAtom>
        <swrl:propertyPredicate>
          <rdf:Description>
            <owl:inverseOf rdf:resource="urn:p"/>
          </rdf:Description>
        </swrl:propertyPredicate>
        <swrl:argument1 rdf:resource="urn:x"/>
        <swrl:argument2 rdf:resource="urn:i"/>
      </swrl:IndividualPropertyAtom>
      <swrl:DatavaluedPropertyAtom>
        <swrl:propertyPredicate rdf:resource="urn:d"/>
        <swrl:argument1 rdf:resource="urn:x"/>
        <swrl:argument2
          rdf:datatype="http://www.w3.org/2001/XMLSchema#integer">007</swrl:argument2>
      </swrl:DatavaluedPropertyAtom>
      <swrl:BuiltinAtom>
        <swrl:builtin rdf:resource="urn:lessThan"/>
        <swrl:arguments rdf:parseType="Collection">
          <rdf:Description rdf:about="urn:x"/>
          <rdf:Description rdf:about="urn:y"/>
        </swrl:arguments>
      </swrl:BuiltinAtom>
      <swrl:SameIndividualAtom>
        <swrl:argument1 rdf:resource="urn:x"/>
        <swrl:argument2 rdf:resource="urn:i"/>
      </swrl:SameIndividualAtom>
    </swrl:body>
    <swrl:head rdf:parseType="Collection">
      <swrl:DifferentIndividualsAtom>
        <swrl:argument1 rdf:resource="urn:x"/>
        <swrl:argument2 rdf:resource="urn:j"/>
      </swrl:DifferentIndividualsAtom>
    </swrl:head>
    <e:note rdf:resource="urn:value"/>
  </swrl:Imp>
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
    "local",
    (
        "RDF",
        "parseType",
        "resource",
        "datatype",
        "Description",
        "li",
        "aboutEach",
        "aboutEachPrefix",
        "bagID",
    ),
)
def test_rdfxml_forbidden_node_property_attributes_fail_at_syntax_boundary(
    local: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}">
  <rdf:Description rdf:about="urn:s" rdf:{local}="value"/>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize(
    "local",
    (
        "RDF",
        "about",
        "Description",
        "li",
        "aboutEach",
        "aboutEachPrefix",
        "bagID",
    ),
)
@pytest.mark.parametrize("object_attribute", ("", 'rdf:resource="urn:o" '))
def test_rdfxml_forbidden_property_element_attributes_fail_at_syntax_boundary(
    local: str,
    object_attribute: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}" xmlns:e="urn:e:">
  <rdf:Description rdf:about="urn:s">
    <e:p {object_attribute}rdf:{local}="value"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


def test_rdfxml_legacy_unqualified_attributes_match_qualified_spelling() -> None:
    def source(prefix: str) -> bytes:
        return f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
 xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
 xmlns:owl="http://www.w3.org/2002/07/owl#" xml:base="urn:legacy">
 <owl:Class {prefix}about="urn:C">
  <rdfs:subClassOf {prefix}resource="urn:D"/>
  <owl:equivalentClass>
   <owl:Class>
    <owl:intersectionOf {prefix}parseType="Collection">
     <owl:Class {prefix}about="urn:D"/>
     <owl:Class {prefix}about="urn:E"/>
    </owl:intersectionOf>
   </owl:Class>
  </owl:equivalentClass>
 </owl:Class>
 <owl:Class {prefix}ID="F"/>
 <rdf:Description {prefix}about="urn:G"
  {prefix}type="http://www.w3.org/2002/07/owl#Class"/>
</rdf:RDF>
""".encode()

    qualified = parse_document(source("rdf:"), format="rdfxml", options=PYTHON_OPTIONS)
    legacy = parse_document(source(""), format="rdfxml", options=PYTHON_OPTIONS)

    assert legacy == qualified
    assert len(legacy.axioms) == 7


@pytest.mark.parametrize(
    "element",
    (
        "<owl:Class rdf:about='urn:C' about='urn:D'/>",
        "<owl:Class rdf:ID='C' ID='D'/>",
        (
            "<rdf:Description rdf:about='urn:C' "
            "rdf:type='http://www.w3.org/2002/07/owl#Class' "
            "type='http://www.w3.org/2002/07/owl#Class'/>"
        ),
        (
            "<owl:Class rdf:about='urn:C'><rdfs:subClassOf "
            "rdf:resource='urn:D' resource='urn:E'/></owl:Class>"
        ),
        (
            "<owl:Class rdf:about='urn:C'><owl:intersectionOf "
            "rdf:parseType='Collection' parseType='Collection'/></owl:Class>"
        ),
    ),
)
def test_rdfxml_qualified_and_legacy_attribute_aliases_are_duplicates(
    element: str,
) -> None:
    source = (
        f"<rdf:RDF xmlns:rdf='{RDF_NAMESPACE}' "
        "xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        "xmlns:owl='http://www.w3.org/2002/07/owl#' xml:base='urn:legacy'>"
        f"{element}</rdf:RDF>"
    ).encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDFXML_SYNTAX"


def test_rdfxml_other_unqualified_attributes_remain_forbidden() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}" xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C" label="value"/>
</rdf:RDF>
""".encode()

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


def test_rdfxml_ignores_reserved_xml_attribute_names() -> None:
    source = b"""<rdf:RDF
 xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#'
 xmlns:owl='http://www.w3.org/2002/07/owl#'
 xmlns:e='urn:xml-attribute:'
 xmlns:XmLmeta='urn:xml-metadata:'
 xmlns:XmLrdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
 xmlns:XML='urn:xml-uppercase:'
 xml:trace='root' xmlroot='root' XmLmeta:trace='root' XML:trace='root'>
 <owl:Ontology rdf:about='urn:xml-attribute:ontology'
  xml:trace='node' XMLnode='node' XmLmeta:trace='node'>
  <rdfs:label xml:trace='property' xmlnewthing='property'
   XmLmeta:trace='property'>Ontology</rdfs:label>
  <rdfs:seeAlso XmLrdf:resource='urn:wrong'/>
  <rdfs:comment rdf:parseType='Literal' xml:trace='outer'
   XmlOuter='outer' XmLmeta:trace='outer'><e:mark
   xml:trace='literal'/></rdfs:comment>
 </owl:Ontology>
</rdf:RDF>"""

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    annotations = {item.property.iri.value: item.value for item in document.ontology_annotations}

    assert annotations["http://www.w3.org/2000/01/rdf-schema#label"] == m.Literal(
        "Ontology",
        m.XSD_STRING,
    )
    assert annotations["http://www.w3.org/2000/01/rdf-schema#seeAlso"] == m.Literal(
        "",
        m.XSD_STRING,
    )
    assert annotations["http://www.w3.org/2000/01/rdf-schema#comment"] == m.Literal(
        '<ns0:mark xmlns:ns0="urn:xml-attribute:" xml:trace="literal"></ns0:mark>',
        m.Datatype(m.IRI(RDF_NAMESPACE + "XMLLiteral")),
    )
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.total_triples == 4
    assert document.rdf_mapping_report.consumed_triples == 4


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


def test_rdfxml_source_map_retains_effective_namespace_bindings() -> None:
    source = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:e="urn:first:"
         xmlns:xml="http://www.w3.org/XML/1998/namespace"
         xmlns="urn:default:">
  <owl:Class xmlns:e="urn:second:" xmlns="" rdf:about="urn:C"/>
</rdf:RDF>
"""

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            preserve_source_map=True,
        ),
    )

    assert document.source_map is not None
    assert dict(document.source_map.prefixes) == {
        "e": "urn:second:",
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "xml": "http://www.w3.org/XML/1998/namespace",
    }


def test_rdfxml_source_map_retains_only_explicit_blank_labels() -> None:
    explicit = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdf:Description rdf:nodeID="lexical-z">
    <owl:sameAs rdf:nodeID="lexical-a"/>
  </rdf:Description>
</rdf:RDF>
"""
    implicit = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdf:Description>
    <owl:sameAs rdf:nodeID="lexical-a"/>
  </rdf:Description>
</rdf:RDF>
"""
    options = LoadOptions(
        backend=BackendPreference.PYTHON,
        preserve_source_map=True,
    )

    explicit_document = parse_document(explicit, format="rdfxml", options=options)
    implicit_document = parse_document(implicit, format="rdfxml", options=options)

    assert explicit_document.source_map is not None
    assert implicit_document.source_map is not None
    assert [
        dict(occurrence.lexical)
        for occurrences in explicit_document.source_map.entries.values()
        for occurrence in occurrences
    ] == [{"blank-label": "lexical-a", "blank-label:2": "lexical-z"}]
    assert [
        dict(occurrence.lexical)
        for occurrences in implicit_document.source_map.entries.values()
        for occurrence in occurrences
    ] == [{"blank-label": "lexical-a"}]
    for label in ("node-1", "generated-1"):
        collision = f"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdf:Description>
    <owl:sameAs rdf:nodeID="{label}"/>
  </rdf:Description>
</rdf:RDF>
""".encode()
        collision_document = parse_document(collision, format="rdfxml", options=options)
        assert collision_document.source_map is not None
        assert [
            dict(occurrence.lexical)
            for occurrences in collision_document.source_map.entries.values()
            for occurrence in occurrences
        ] == [{"blank-label": label}]
        collision_axiom = next(iter(collision_document.axioms))
        assert len(
            {
                value
                for value in m.walk(collision_axiom)
                if isinstance(value, m.AnonymousIndividual)
            }
        ) == 2
        provenance_collision = f"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdf:Description rdf:nodeID="{label}"/>
  <rdf:Description>
    <owl:sameAs rdf:resource="urn:named"/>
  </rdf:Description>
</rdf:RDF>
""".encode()
        provenance_document = parse_document(
            provenance_collision,
            format="rdfxml",
            options=options,
        )
        assert provenance_document.source_map is not None
        assert not any(
            "blank-label" in occurrence.lexical
            for occurrences in provenance_document.source_map.entries.values()
            for occurrence in occurrences
        )


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


def test_rdfxml_generated_membership_iris_enforce_utf8_byte_limits() -> None:
    members = "".join("<rdf:li rdf:resource='urn:o'/>" for _ in range(100))
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}" xmlns:e="urn:e:">
  <e:C rdf:about="urn:s">{members}</e:C>
</rdf:RDF>
""".encode()

    partial = PythonParser().parse(
        source,
        format="rdfxml",
        document_iri=None,
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_iri_bytes=47),
        ),
        allow_partial_rdf_mapping=True,
    )
    assert partial.rdf_mapping_report is not None
    assert partial.rdf_mapping_report.total_triples == 101

    with pytest.raises(ResourceLimitError) as raised:
        PythonParser().parse(
            source,
            format="rdfxml",
            document_iri=None,
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_iri_bytes=46),
            ),
            allow_partial_rdf_mapping=True,
        )
    assert raised.value.limit == "max_iri_bytes"


def test_rdf_mapping_enforces_canonical_model_nesting_limits() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}" xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <owl:equivalentClass rdf:nodeID="x1"/>
  </owl:Class>
  <owl:Class rdf:nodeID="x1"><owl:complementOf rdf:nodeID="x2"/></owl:Class>
  <owl:Class rdf:nodeID="x2"><owl:complementOf rdf:nodeID="x3"/></owl:Class>
  <owl:Class rdf:nodeID="x3"><owl:complementOf rdf:resource="urn:D"/></owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_nesting_depth=5),
        ),
    )
    assert len(document.axioms) == 2

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_nesting_depth=4),
            ),
        )
    assert raised.value.limit == "max_nesting_depth"


def test_rdf_mapping_enforces_the_distinct_axiom_limit() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}" xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C"/>
  <owl:Class rdf:about="urn:D"/>
</rdf:RDF>
""".encode()

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_axioms=2),
        ),
    )
    assert len(document.axioms) == 2

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_axioms=1),
            ),
        )
    assert raised.value.limit == "max_axioms"


def test_rdf_mapping_enforces_raw_rdf_list_sequence_arity() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <rdfs:subClassOf>
      <owl:Class>
        <owl:oneOf rdf:parseType="Collection">
          <rdf:Description rdf:about="urn:i"/>
          <rdf:Description rdf:about="urn:i"/>
        </owl:oneOf>
      </owl:Class>
    </rdfs:subClassOf>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_sequence_arity=2),
        ),
    )
    assert len(document.axioms) == 2

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_sequence_arity=1),
            ),
        )
    assert raised.value.limit == "max_sequence_arity"


@pytest.mark.parametrize(
    ("operator", "members", "expected"),
    (
        ("intersectionOf", "", m.OWL_THING),
        ("unionOf", "", m.OWL_NOTHING),
        ("oneOf", "", m.OWL_NOTHING),
        ("intersectionOf", '<owl:Class rdf:about="urn:B"/>', m.Class(m.IRI("urn:B"))),
        ("unionOf", '<owl:Class rdf:about="urn:B"/>', m.Class(m.IRI("urn:B"))),
    ),
)
def test_rdf_mapping_handles_owl1_class_list_compatibility(
    operator: str,
    members: str,
    expected: m.Class,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:A">
    <rdfs:subClassOf>
      <owl:Class><owl:{operator} rdf:parseType="Collection">
        {members}
      </owl:{operator}></owl:Class>
    </rdfs:subClassOf>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert m.SubClassOf(m.Class(m.IRI("urn:A")), expected) in document.axioms


def test_rdf_mapping_rejects_unmarked_empty_class_list() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:A">
    <rdfs:subClassOf>
      <rdf:Description>
        <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
      </rdf:Description>
    </rdfs:subClassOf>
  </owl:Class>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_UNSUPPORTED"


@pytest.mark.parametrize(
    ("constructor", "expected"),
    (
        (
            '<owl:complementOf rdf:resource="urn:A"/>',
            m.ObjectComplementOf(m.Class(m.IRI("urn:A"))),
        ),
        (
            f'<owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>',
            m.OWL_THING,
        ),
        (
            '<owl:unionOf rdf:parseType="Collection">'
            '<owl:Class rdf:about="urn:A"/>'
            "</owl:unionOf>",
            m.Class(m.IRI("urn:A")),
        ),
        (
            f'<owl:oneOf rdf:resource="{RDF_NAMESPACE}nil"/>',
            m.OWL_NOTHING,
        ),
        (
            '<owl:oneOf rdf:parseType="Collection">'
            '<owl:NamedIndividual rdf:about="urn:i"/>'
            "</owl:oneOf>",
            m.ObjectOneOf(m.CanonicalSet((m.NamedIndividual(m.IRI("urn:i")),))),
        ),
    ),
)
def test_rdf_mapping_maps_owl1_named_class_constructors(
    constructor: str,
    expected: m.ClassExpression,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">{constructor}</owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert m.EquivalentClasses(
        m.CanonicalSet((m.Class(m.IRI("urn:C")), expected))
    ) in document.axioms


def test_rdf_mapping_rejects_anonymous_owl1_named_enumeration_member() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <owl:oneOf rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="anonymous"/>
    </owl:oneOf>
  </owl:Class>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_UNSUPPORTED"


@pytest.mark.parametrize(
    "body",
    (
        (
            '<owl:Class rdf:about="urn:C">'
            '<rdf:type rdf:resource="http://www.w3.org/2000/01/rdf-schema#Class"/>'
            "</owl:Class>"
        ),
        (
            '<owl:ObjectProperty rdf:about="urn:p">'
            f'<rdf:type rdf:resource="{RDF_NAMESPACE}Property"/>'
            "</owl:ObjectProperty>"
        ),
    ),
)
def test_rdf_mapping_consumes_redundant_owl1_types(body: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert len(tuple(document.iter_axioms(m.Declaration))) == 1
    assert len(document.axioms) == 1


@pytest.mark.parametrize(
    "legacy_type",
    (RDF_NAMESPACE + "Property", "http://www.w3.org/2000/01/rdf-schema#Class"),
)
def test_rdf_mapping_rejects_standalone_owl1_structural_types(legacy_type: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}">
  <rdf:Description rdf:about="urn:value">
    <rdf:type rdf:resource="{legacy_type}"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


def test_rdf_mapping_maps_empty_owl1_data_range() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:DatatypeProperty rdf:about="urn:d"/>
  <rdf:Description rdf:about="urn:A">
    <rdfs:subClassOf>
      <owl:Restriction>
        <owl:onProperty rdf:resource="urn:d"/>
        <owl:allValuesFrom>
          <owl:DataRange>
            <rdf:type rdf:resource="http://www.w3.org/2000/01/rdf-schema#Class"/>
            <owl:oneOf rdf:resource="{RDF_NAMESPACE}nil"/>
          </owl:DataRange>
        </owl:allValuesFrom>
      </owl:Restriction>
    </rdfs:subClassOf>
  </rdf:Description>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    expected = m.SubClassOf(
        m.Class(m.IRI("urn:A")),
        m.DataAllValuesFrom(
            (m.DataProperty(m.IRI("urn:d")),),
            m.DataComplementOf(m.RDFS_LITERAL),
        ),
    )
    assert expected in document.axioms


def test_rdf_mapping_rejects_empty_modern_data_enumeration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:DatatypeProperty rdf:about="urn:d"/>
  <rdf:Description rdf:about="urn:A">
    <rdfs:subClassOf>
      <owl:Restriction>
        <owl:onProperty rdf:resource="urn:d"/>
        <owl:allValuesFrom>
          <rdfs:Datatype><owl:oneOf rdf:resource="{RDF_NAMESPACE}nil"/></rdfs:Datatype>
        </owl:allValuesFrom>
      </owl:Restriction>
    </rdfs:subClassOf>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_UNSUPPORTED"


def test_rdf_mapping_maps_owl1_declarations_characteristics_and_deprecation() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <rdf:type rdf:resource="http://www.w3.org/2000/01/rdf-schema#Class"/>
  </owl:Class>
  <owl:ObjectProperty rdf:about="urn:p">
    <rdf:type rdf:resource="{RDF_NAMESPACE}Property"/>
  </owl:ObjectProperty>
  <owl:OntologyProperty rdf:about="urn:ap">
    <rdf:type rdf:resource="{RDF_NAMESPACE}Property"/>
  </owl:OntologyProperty>
  <rdf:Description rdf:about="urn:inverse">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#InverseFunctionalProperty"/>
    <rdf:type rdf:resource="{RDF_NAMESPACE}Property"/>
  </rdf:Description>
  <rdf:Description rdf:about="urn:symmetric">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#SymmetricProperty"/>
    <rdf:type rdf:resource="{RDF_NAMESPACE}Property"/>
  </rdf:Description>
  <rdf:Description rdf:about="urn:transitive">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#TransitiveProperty"/>
    <rdf:type rdf:resource="{RDF_NAMESPACE}Property"/>
  </rdf:Description>
  <owl:Class rdf:about="urn:Old">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#DeprecatedClass"/>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 14
    assert document.rdf_mapping_report.consumed_triples == 14
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(tuple(document.iter_axioms(m.Declaration))) == 7
    assert (
        m.InverseFunctionalObjectProperty(m.ObjectProperty(m.IRI("urn:inverse")))
        in document.axioms
    )
    assert m.SymmetricObjectProperty(m.ObjectProperty(m.IRI("urn:symmetric"))) in document.axioms
    assert m.TransitiveObjectProperty(m.ObjectProperty(m.IRI("urn:transitive"))) in document.axioms
    assert m.AnnotationAssertion(
        m.AnnotationProperty(m.IRI("http://www.w3.org/2002/07/owl#deprecated")),
        m.IRI("urn:Old"),
        m.Literal(
            "true",
            m.Datatype(m.IRI("http://www.w3.org/2001/XMLSchema#boolean")),
        ),
    ) in document.axioms
    assert len(document.axioms) == 11


def test_rdf_mapping_rejects_owl1_property_marker_on_a_nonproperty() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:NamedIndividual rdf:about="urn:i">
    <rdf:type rdf:resource="{RDF_NAMESPACE}Property"/>
  </owl:NamedIndividual>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


def test_rdf_mapping_does_not_duplicate_explicit_inferred_declaration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:ObjectProperty rdf:about="urn:p">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#SymmetricProperty"/>
  </owl:ObjectProperty>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert len(tuple(document.iter_axioms(m.Declaration))) == 1
    assert len(document.axioms) == 2


def test_rdf_mapping_consumes_detached_inverse_property_expression() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:ObjectProperty rdf:about="urn:q"/>
  <rdf:Description rdf:nodeID="inverse">
    <owl:inverseOf rdf:resource="urn:q"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 2
    assert document.rdf_mapping_report.consumed_triples == 2
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(tuple(document.iter_axioms(m.Declaration))) == 1
    assert len(document.axioms) == 1


@pytest.mark.parametrize(
    "body",
    (
        """
  <rdf:Description rdf:nodeID="inverse">
    <owl:inverseOf rdf:resource="urn:undeclared"/>
  </rdf:Description>
""",
        """
  <owl:ObjectProperty rdf:about="urn:q"/>
  <rdf:Description rdf:nodeID="inverse">
    <owl:inverseOf rdf:nodeID="anonymous"/>
  </rdf:Description>
""",
    ),
)
def test_rdf_mapping_rejects_unestablished_detached_inverse_property_expression(
    body: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


def test_rdf_mapping_rejects_ambiguous_detached_inverse_property_expression() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:ObjectProperty rdf:about="urn:q"/>
  <owl:ObjectProperty rdf:about="urn:r"/>
  <rdf:Description rdf:nodeID="inverse">
    <owl:inverseOf rdf:resource="urn:q"/>
    <owl:inverseOf rdf:resource="urn:r"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_CARDINALITY"


def test_rdf_mapping_consumes_detached_class_complement() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C"/>
  <owl:Class rdf:nodeID="complement">
    <owl:complementOf rdf:resource="urn:C"/>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 3
    assert document.rdf_mapping_report.consumed_triples == 3
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(tuple(document.iter_axioms(m.Declaration))) == 1
    assert len(document.axioms) == 1


@pytest.mark.parametrize("target", ("Thing", "Nothing"))
def test_rdf_mapping_consumes_detached_builtin_class_complement(
    target: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
  <owl:Class rdf:nodeID="complement">
    <owl:complementOf rdf:resource="{OWL_NAMESPACE}{target}"/>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 2
    assert document.rdf_mapping_report.consumed_triples == 2
    assert document.rdf_mapping_report.unconsumed == ()
    assert not document.axioms


@pytest.mark.parametrize(
    "target",
    (
        OWL_NAMESPACE + "Class",
        OWL_NAMESPACE + "real",
        OWL_NAMESPACE + "Thingy",
    ),
)
def test_rdf_mapping_rejects_near_builtin_class_complement(target: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
  <owl:Class rdf:nodeID="complement">
    <owl:complementOf rdf:resource="{target}"/>
  </owl:Class>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    "body",
    (
        """
  <owl:Class rdf:about="urn:C"/>
  <rdf:Description rdf:nodeID="complement">
    <owl:complementOf rdf:resource="urn:C"/>
  </rdf:Description>
""",
        """
  <owl:Class rdf:nodeID="complement">
    <owl:complementOf rdf:resource="urn:undeclared"/>
  </owl:Class>
""",
        """
  <owl:Class rdf:about="urn:C"/>
  <owl:Class rdf:nodeID="complement">
    <owl:complementOf rdf:nodeID="anonymous"/>
  </owl:Class>
""",
        """
  <owl:Class rdf:about="urn:C"/>
  <owl:Class rdf:nodeID="left">
    <owl:complementOf rdf:nodeID="right"/>
  </owl:Class>
  <owl:Class rdf:nodeID="right">
    <owl:complementOf rdf:nodeID="left"/>
  </owl:Class>
""",
    ),
)
def test_rdf_mapping_rejects_unestablished_detached_class_complement(body: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


def test_rdf_mapping_rejects_ambiguous_detached_class_complement() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C"/>
  <owl:Class rdf:about="urn:D"/>
  <owl:Class rdf:nodeID="complement">
    <owl:complementOf rdf:resource="urn:C"/>
    <owl:complementOf rdf:resource="urn:D"/>
  </owl:Class>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_CARDINALITY"


@pytest.mark.parametrize("operator", ("intersectionOf", "unionOf"))
def test_rdf_mapping_consumes_detached_empty_class_boolean(operator: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:nodeID="expression">
    <owl:{operator} rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 2
    assert document.rdf_mapping_report.consumed_triples == 2
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(document.axioms) == 0


@pytest.mark.parametrize(
    ("operator", "members"),
    (
        ("intersectionOf", ("A",)),
        ("unionOf", ("A",)),
        ("intersectionOf", ("A", "B")),
        ("unionOf", ("A", "B")),
    ),
)
def test_rdf_mapping_consumes_detached_named_class_boolean(
    operator: str,
    members: tuple[str, ...],
) -> None:
    declarations = "".join(
        f'<owl:Class rdf:about="urn:{member}"/>' for member in members
    )
    items = "".join(
        f'<rdf:Description rdf:about="urn:{member}"/>' for member in members
    )
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  {declarations}
  <owl:Class rdf:nodeID="expression">
    <owl:{operator} rdf:parseType="Collection">{items}</owl:{operator}>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    expected_triples = 2 + 3 * len(members)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == expected_triples
    assert document.rdf_mapping_report.consumed_triples == expected_triples
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(tuple(document.iter_axioms(m.Declaration))) == len(members)
    assert len(document.axioms) == len(members)


@pytest.mark.parametrize("operator", ("intersectionOf", "unionOf"))
def test_rdf_mapping_consumes_detached_builtin_named_class_boolean(
    operator: str,
) -> None:
    items = "".join(
        f'<rdf:Description rdf:about="{OWL_NAMESPACE}{target}"/>'
        for target in ("Thing", "Nothing")
    )
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
  <owl:Class rdf:nodeID="expression">
    <owl:{operator} rdf:parseType="Collection">{items}</owl:{operator}>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 6
    assert document.rdf_mapping_report.consumed_triples == 6
    assert document.rdf_mapping_report.unconsumed == ()
    assert not document.axioms


@pytest.mark.parametrize(
    "target",
    (
        OWL_NAMESPACE + "Class",
        OWL_NAMESPACE + "real",
        OWL_NAMESPACE + "Thingy",
    ),
)
def test_rdf_mapping_rejects_near_builtin_named_class_boolean(
    target: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="{OWL_NAMESPACE}Thing"/>
      <rdf:Description rdf:about="{target}"/>
    </owl:intersectionOf>
  </owl:Class>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    ("operator", "members"),
    (
        ("intersectionOf", ("A",)),
        ("unionOf", ("A",)),
        ("intersectionOf", ("A", "B")),
        ("unionOf", ("A", "B")),
    ),
)
def test_rdf_mapping_preserves_named_class_boolean_axiom(
    operator: str,
    members: tuple[str, ...],
) -> None:
    declarations = "".join(
        f'<owl:Class rdf:about="urn:{member}"/>' for member in members
    )
    items = "".join(
        f'<rdf:Description rdf:about="urn:{member}"/>' for member in members
    )
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <owl:{operator} rdf:parseType="Collection">{items}</owl:{operator}>
  </owl:Class>
  {declarations}
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    operands = m.CanonicalSet(m.Class(m.IRI(f"urn:{member}")) for member in members)
    expected = (
        next(iter(operands))
        if len(operands) == 1
        else m.ObjectIntersectionOf(operands)
        if operator == "intersectionOf"
        else m.ObjectUnionOf(operands)
    )
    assert (
        m.EquivalentClasses(m.CanonicalSet((m.Class(m.IRI("urn:C")), expected))) in document.axioms
    )
    assert len(document.axioms) == len(members) + 2
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.total_triples == 2 + 3 * len(members)
    assert document.rdf_mapping_report.consumed_triples == 2 + 3 * len(members)


@pytest.mark.parametrize(
    ("operator", "expected"),
    (
        ("intersectionOf", m.OWL_THING),
        ("unionOf", m.OWL_NOTHING),
    ),
)
def test_rdf_mapping_preserves_named_empty_class_boolean_axiom(
    operator: str,
    expected: m.Class,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <owl:{operator} rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:Class>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert (
        m.EquivalentClasses(m.CanonicalSet((m.Class(m.IRI("urn:C")), expected))) in document.axioms
    )
    assert len(document.axioms) == 2


@pytest.mark.parametrize(
    ("body", "code"),
    (
        (
            """
  <owl:Class rdf:about="urn:A"/>
  <rdf:Description rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
    </owl:intersectionOf>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:undeclared"/>
    </owl:intersectionOf>
  </owl:Class>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <owl:Class rdf:nodeID="inner">
    <owl:unionOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:Class>
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:nodeID="inner"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:about="urn:B"/>
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:first rdf:resource="urn:B"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
    <rdf:rest rdf:nodeID="values"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="values"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:nodeID="expression">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Restriction"/>
    <owl:intersectionOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
    </owl:intersectionOf>
    <owl:complementOf rdf:resource="urn:A"/>
  </owl:Class>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            f"""
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="left"/>
    <owl:intersectionOf rdf:nodeID="right"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="left">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_CARDINALITY",
        ),
        (
            f"""
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:about="urn:B"/>
  <owl:Class rdf:nodeID="left-expression">
    <owl:intersectionOf rdf:nodeID="left"/>
  </owl:Class>
  <owl:Class rdf:nodeID="right-expression">
    <owl:unionOf rdf:nodeID="right"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="left">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first rdf:resource="urn:B"/>
    <rdf:rest rdf:nodeID="tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="tail">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
    ),
)
def test_rdf_mapping_preserves_detached_named_class_boolean_boundary(
    body: str,
    code: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises((OntologySyntaxError, UnsupportedSyntaxError)) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == code


def test_detached_named_class_boolean_precheck_does_not_claim_invalid_lists() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:A"/>
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="values"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    partial = PythonParser().parse(
        source,
        format="rdfxml",
        document_iri=None,
        options=PYTHON_OPTIONS,
        allow_partial_rdf_mapping=True,
    )
    assert partial.rdf_mapping_report is not None
    assert not partial.rdf_mapping_report.conformant
    assert partial.rdf_mapping_report.total_triples == 5
    assert partial.rdf_mapping_report.consumed_triples == 1
    assert len(partial.rdf_mapping_report.unconsumed) == 4


@pytest.mark.parametrize(
    ("body", "code"),
    (
        (
            f"""
  <rdf:Description rdf:nodeID="expression">
    <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
    <owl:unionOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:Class>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            f"""
  <owl:Class rdf:nodeID="expression">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Restriction"/>
    <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:Class>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <rdf:Description rdf:nodeID="expression">
    <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
    <owl:intersectionOf rdf:resource="urn:not-a-list"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <owl:Class rdf:nodeID="expression">
    <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
    <owl:intersectionOf rdf:resource="urn:not-a-list"/>
  </owl:Class>
""",
            "RDF_MAPPING_CARDINALITY",
        ),
    ),
)
def test_rdf_mapping_preserves_detached_empty_class_boolean_boundary(
    body: str,
    code: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises((OntologySyntaxError, UnsupportedSyntaxError)) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("body", "total_triples"),
    (
        (
            f"""
  <owl:Class rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:i"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            4,
        ),
        (
            f"""
  <owl:Class rdf:nodeID="range">
    <owl:oneOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:Class>
""",
            2,
        ),
    ),
)
def test_rdf_mapping_consumes_detached_object_enumeration(
    body: str,
    total_triples: int,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == total_triples
    assert document.rdf_mapping_report.consumed_triples == total_triples
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(document.axioms) == 0


def test_rdf_mapping_preserves_named_object_enumeration_axiom() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C">
    <owl:oneOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:i"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    enumeration = m.ObjectOneOf(
        m.CanonicalSet((m.NamedIndividual(m.IRI("urn:i")),))
    )
    assert m.EquivalentClasses(
        m.CanonicalSet((m.Class(m.IRI("urn:C")), enumeration))
    ) in document.axioms
    assert len(document.axioms) == 2


def test_rdf_mapping_rejects_markerless_detached_object_enumeration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdf:Description rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:i"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    "body",
    (
        f"""
  <owl:Class rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>one</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
        f"""
  <owl:Class rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
    <owl:complementOf rdf:resource="urn:C"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:i"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
    ),
)
def test_rdf_mapping_rejects_invalid_detached_object_enumeration(body: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_UNSUPPORTED"


def test_rdf_mapping_rejects_ambiguous_detached_object_enumeration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="left"/>
    <owl:oneOf rdf:nodeID="right"/>
  </owl:Class>
  <rdf:Description rdf:nodeID="left">
    <rdf:first rdf:resource="urn:left"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first rdf:resource="urn:right"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_CARDINALITY"


@pytest.mark.parametrize(
    ("operator", "members"),
    (
        ("intersectionOf", ("A", "B")),
        ("unionOf", ("A", "B")),
        ("intersectionOf", ("A", "A")),
        ("unionOf", ("A", "A")),
    ),
)
def test_rdf_mapping_consumes_detached_named_data_boolean(
    operator: str,
    members: tuple[str, ...],
) -> None:
    declared = tuple(sorted(set(members)))
    declarations = "".join(
        f'<rdfs:Datatype rdf:about="urn:{member}"/>' for member in declared
    )
    items = "".join(
        f'<rdf:Description rdf:about="urn:{member}"/>' for member in members
    )
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  {declarations}
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:{operator} rdf:parseType="Collection">{items}</owl:{operator}>
  </rdfs:Datatype>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    expected_triples = len(declared) + 2 + 2 * len(members)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == expected_triples
    assert document.rdf_mapping_report.consumed_triples == expected_triples
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(tuple(document.iter_axioms(m.Declaration))) == len(declared)
    assert len(document.axioms) == len(declared)


@pytest.mark.parametrize("operator", ("intersectionOf", "unionOf"))
def test_rdf_mapping_consumes_detached_builtin_named_data_boolean(
    operator: str,
) -> None:
    items = "".join(
        f'<rdf:Description rdf:about="{datatype}"/>'
        for datatype in OWL2_BUILTIN_DATATYPES
    )
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="{RDFS_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:{operator} rdf:parseType="Collection">{items}</owl:{operator}>
  </rdfs:Datatype>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    expected_triples = 2 + 2 * len(OWL2_BUILTIN_DATATYPES)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == expected_triples
    assert document.rdf_mapping_report.consumed_triples == expected_triples
    assert document.rdf_mapping_report.unconsumed == ()
    assert not document.axioms


@pytest.mark.parametrize(
    "datatype",
    (
        XSD_NAMESPACE + "duration",
        XSD_NAMESPACE + "anyType",
        RDF_NAMESPACE + "langString",
        OWL_NAMESPACE + "realNumber",
        RDFS_NAMESPACE + "Datatype",
    ),
)
def test_rdf_mapping_rejects_near_builtin_named_data_boolean(
    datatype: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="{RDFS_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="{XSD_NAMESPACE}string"/>
      <rdf:Description rdf:about="{datatype}"/>
    </owl:intersectionOf>
  </rdfs:Datatype>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    ("body", "code"),
    (
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:about="urn:B"/>
  <rdf:Description rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
      <rdf:Description rdf:about="urn:B"/>
    </owl:intersectionOf>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:about="urn:B"/>
  <rdfs:Datatype rdf:about="urn:expression">
    <owl:unionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
      <rdf:Description rdf:about="urn:B"/>
    </owl:unionOf>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:unionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
    </owl:unionOf>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
      <rdf:Description rdf:about="urn:undeclared"/>
    </owl:intersectionOf>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
      <rdf:Description rdf:nodeID="anonymous"/>
    </owl:intersectionOf>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="tail">
    <rdf:first>literal</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:about="urn:B"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:first rdf:resource="urn:B"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="values"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:about="urn:B"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#DataRange"/>
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
      <rdf:Description rdf:about="urn:B"/>
    </owl:intersectionOf>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:about="urn:B"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:A"/>
      <rdf:Description rdf:about="urn:B"/>
    </owl:intersectionOf>
    <owl:datatypeComplementOf rdf:resource="urn:A"/>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:about="urn:B"/>
  <rdfs:Datatype rdf:nodeID="left-expression">
    <owl:intersectionOf rdf:nodeID="left"/>
  </rdfs:Datatype>
  <rdfs:Datatype rdf:nodeID="right-expression">
    <owl:unionOf rdf:nodeID="right"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="left">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first rdf:resource="urn:B"/>
    <rdf:rest rdf:nodeID="tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="tail">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:about="urn:B"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="left"/>
    <owl:intersectionOf rdf:nodeID="right"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="left">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="left-tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="left-tail">
    <rdf:first rdf:resource="urn:B"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="right-tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right-tail">
    <rdf:first rdf:resource="urn:B"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_CARDINALITY",
        ),
    ),
)
def test_rdf_mapping_preserves_detached_named_data_boolean_boundary(
    body: str,
    code: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises((OntologySyntaxError, UnsupportedSyntaxError)) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == code


def test_detached_named_data_boolean_precheck_does_not_claim_invalid_lists() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdfs:Datatype rdf:about="urn:A"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:intersectionOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:A"/>
    <rdf:rest rdf:nodeID="values"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    partial = PythonParser().parse(
        source,
        format="rdfxml",
        document_iri=None,
        options=PYTHON_OPTIONS,
        allow_partial_rdf_mapping=True,
    )
    assert partial.rdf_mapping_report is not None
    assert not partial.rdf_mapping_report.conformant
    assert partial.rdf_mapping_report.total_triples == 5
    assert partial.rdf_mapping_report.consumed_triples == 1
    assert len(partial.rdf_mapping_report.unconsumed) == 4


@pytest.mark.parametrize(
    ("members", "facet_definitions"),
    (
        (
            ("lower",),
            (("lower", "minInclusive", "1"),),
        ),
        (
            ("lower", "upper"),
            (
                ("lower", "minInclusive", "1"),
                ("upper", "maxExclusive", "10"),
            ),
        ),
        (
            ("lower", "lower"),
            (("lower", "minInclusive", "1"),),
        ),
    ),
)
def test_rdf_mapping_consumes_detached_datatype_restriction(
    members: tuple[str, ...],
    facet_definitions: tuple[tuple[str, str, str], ...],
) -> None:
    items = "".join(
        f'<rdf:Description rdf:nodeID="{member}"/>' for member in members
    )
    facets = "".join(
        f"""\
  <rdf:Description rdf:nodeID="{node}">
    <xsd:{predicate} rdf:datatype="{XSD_NAMESPACE}integer">{lexical}</xsd:{predicate}>
  </rdf:Description>
"""
        for node, predicate, lexical in facet_definitions
    )
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:xsd="{XSD_NAMESPACE}">
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      {items}
    </owl:withRestrictions>
  </rdfs:Datatype>
{facets}</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    expected_triples = 4 + 2 * len(members) + len(facet_definitions)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == expected_triples
    assert document.rdf_mapping_report.consumed_triples == expected_triples
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(tuple(document.iter_axioms(m.Declaration))) == 1
    assert len(document.axioms) == 1


@pytest.mark.parametrize("base", OWL2_BUILTIN_DATATYPES)
def test_rdf_mapping_consumes_detached_builtin_datatype_restriction(base: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="{RDFS_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}"
         xmlns:xsd="{XSD_NAMESPACE}">
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="{base}"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet">
    <xsd:minInclusive>1</xsd:minInclusive>
  </rdf:Description>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 6
    assert document.rdf_mapping_report.consumed_triples == 6
    assert document.rdf_mapping_report.unconsumed == ()
    assert not document.axioms


@pytest.mark.parametrize(
    "base",
    (
        XSD_NAMESPACE + "duration",
        XSD_NAMESPACE + "anyType",
        RDF_NAMESPACE + "langString",
        OWL_NAMESPACE + "realNumber",
        RDFS_NAMESPACE + "Datatype",
    ),
)
def test_rdf_mapping_rejects_near_builtin_datatype_restriction(base: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="{RDFS_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}"
         xmlns:xsd="{XSD_NAMESPACE}">
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="{base}"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet">
    <xsd:minInclusive>1</xsd:minInclusive>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    ("body", "code"),
    (
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdf:Description rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdf:Description>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:about="urn:expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:undeclared"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <owl:Class rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:nodeID="base"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype>urn:D</owl:onDatatype>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdfs:Datatype>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:about="urn:facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:about="urn:facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>facet</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet">
    <xsd:minInclusive rdf:resource="urn:value"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:nodeID="left"/>
    <rdf:first rdf:nodeID="right"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="left"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
  <rdf:Description rdf:nodeID="right"><xsd:maxExclusive>10</xsd:maxExclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:nodeID="facet"/>
    <rdf:rest rdf:nodeID="values"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet">
    <xsd:minInclusive>1</xsd:minInclusive>
    <xsd:note rdf:resource="urn:extra"/>
  </rdf:Description>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:about="urn:E"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:onDatatype rdf:resource="urn:E"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_CARDINALITY",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="left-facet"/>
    </owl:withRestrictions>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="right-facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="left-facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
  <rdf:Description rdf:nodeID="right-facet">
    <xsd:maxExclusive>10</xsd:maxExclusive>
  </rdf:Description>
""",
            "RDF_MAPPING_CARDINALITY",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#DataRange"/>
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
    <owl:datatypeComplementOf rdf:resource="urn:D"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:parseType="Collection">
      <rdf:Description rdf:nodeID="facet"/>
    </owl:withRestrictions>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="facet">
    <xsd:minInclusive>1</xsd:minInclusive>
    <xsd:maxExclusive>10</xsd:maxExclusive>
  </rdf:Description>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="left-expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:nodeID="left"/>
  </rdfs:Datatype>
  <rdfs:Datatype rdf:nodeID="right-expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:nodeID="right"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="left">
    <rdf:first rdf:nodeID="left-facet"/>
    <rdf:rest rdf:nodeID="tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first rdf:nodeID="right-facet"/>
    <rdf:rest rdf:nodeID="tail"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="tail">
    <rdf:first rdf:nodeID="tail-facet"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="left-facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
  <rdf:Description rdf:nodeID="right-facet"><xsd:minInclusive>2</xsd:minInclusive></rdf:Description>
  <rdf:Description rdf:nodeID="tail-facet"><xsd:maxExclusive>10</xsd:maxExclusive></rdf:Description>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
        (
            f"""
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:nodeID="values"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
    <xsd:minInclusive>1</xsd:minInclusive>
  </rdf:Description>
""",
            "RDF_MAPPING_UNSUPPORTED",
        ),
    ),
)
def test_rdf_mapping_preserves_detached_datatype_restriction_boundary(
    body: str,
    code: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:xsd="{XSD_NAMESPACE}">{body}</rdf:RDF>
""".encode()

    with pytest.raises((OntologySyntaxError, UnsupportedSyntaxError)) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == code


def test_detached_datatype_restriction_precheck_does_not_claim_invalid_lists() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:xsd="{XSD_NAMESPACE}">
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="expression">
    <owl:onDatatype rdf:resource="urn:D"/>
    <owl:withRestrictions rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:nodeID="facet"/>
    <rdf:rest rdf:nodeID="values"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="facet"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description>
</rdf:RDF>
""".encode()

    partial = PythonParser().parse(
        source,
        format="rdfxml",
        document_iri=None,
        options=PYTHON_OPTIONS,
        allow_partial_rdf_mapping=True,
    )
    assert partial.rdf_mapping_report is not None
    assert not partial.rdf_mapping_report.conformant
    assert partial.rdf_mapping_report.total_triples == 7
    assert partial.rdf_mapping_report.consumed_triples == 1
    assert len(partial.rdf_mapping_report.unconsumed) == 6


def test_rdf_mapping_consumes_detached_datatype_complement() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="complement">
    <owl:datatypeComplementOf rdf:resource="urn:D"/>
  </rdfs:Datatype>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 3
    assert document.rdf_mapping_report.consumed_triples == 3
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(tuple(document.iter_axioms(m.Declaration))) == 1
    assert len(document.axioms) == 1


def test_rdf_mapping_consumes_detached_builtin_datatype_complements() -> None:
    expressions = "".join(
        f"""\
  <rdfs:Datatype rdf:nodeID="complement-{index}">
    <owl:datatypeComplementOf rdf:resource="{datatype}"/>
  </rdfs:Datatype>
"""
        for index, datatype in enumerate(OWL2_BUILTIN_DATATYPES)
    )
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="{RDFS_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
{expressions}</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    expected_triples = 2 * len(OWL2_BUILTIN_DATATYPES)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == expected_triples
    assert document.rdf_mapping_report.consumed_triples == expected_triples
    assert document.rdf_mapping_report.unconsumed == ()
    assert not document.axioms


@pytest.mark.parametrize(
    "datatype",
    (
        XSD_NAMESPACE + "duration",
        XSD_NAMESPACE + "anyType",
        RDF_NAMESPACE + "langString",
        OWL_NAMESPACE + "realNumber",
        RDFS_NAMESPACE + "Datatype",
    ),
)
def test_rdf_mapping_rejects_near_builtin_datatype_complement(
    datatype: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="{RDFS_NAMESPACE}"
         xmlns:owl="{OWL_NAMESPACE}">
  <rdfs:Datatype rdf:nodeID="complement">
    <owl:datatypeComplementOf rdf:resource="{datatype}"/>
  </rdfs:Datatype>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    "body",
    (
        """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdf:Description rdf:nodeID="complement">
    <owl:datatypeComplementOf rdf:resource="urn:D"/>
  </rdf:Description>
""",
        """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:about="urn:complement">
    <owl:datatypeComplementOf rdf:resource="urn:D"/>
  </rdfs:Datatype>
""",
        """
  <rdfs:Datatype rdf:nodeID="complement">
    <owl:datatypeComplementOf rdf:resource="urn:undeclared"/>
  </rdfs:Datatype>
""",
        """
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="complement">
    <owl:datatypeComplementOf rdf:nodeID="anonymous"/>
  </rdfs:Datatype>
""",
        """
  <rdfs:Datatype rdf:nodeID="left">
    <owl:datatypeComplementOf rdf:nodeID="right"/>
  </rdfs:Datatype>
  <rdfs:Datatype rdf:nodeID="right">
    <owl:datatypeComplementOf rdf:nodeID="left"/>
  </rdfs:Datatype>
""",
    ),
)
def test_rdf_mapping_rejects_unestablished_detached_datatype_complement(
    body: str,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


def test_rdf_mapping_rejects_ambiguous_detached_datatype_complement() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdfs:Datatype rdf:about="urn:C"/>
  <rdfs:Datatype rdf:about="urn:D"/>
  <rdfs:Datatype rdf:nodeID="complement">
    <owl:datatypeComplementOf rdf:resource="urn:C"/>
    <owl:datatypeComplementOf rdf:resource="urn:D"/>
  </rdfs:Datatype>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_CARDINALITY"


def test_rdf_mapping_consumes_detached_data_enumeration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdfs:Datatype rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>one</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == 4
    assert document.rdf_mapping_report.consumed_triples == 4
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(document.axioms) == 0


@pytest.mark.parametrize(
    "body",
    (
        f"""
  <rdf:Description rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>one</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
        f"""
  <rdfs:Datatype rdf:about="urn:range">
    <owl:oneOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>one</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
    ),
)
def test_rdf_mapping_rejects_unestablished_detached_data_enumeration(body: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    "body",
    (
        f"""
  <rdfs:Datatype rdf:nodeID="range">
    <owl:oneOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdfs:Datatype>
""",
        f"""
  <rdfs:Datatype rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:value"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
        f"""
  <rdfs:Datatype rdf:nodeID="range">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#DataRange"/>
    <owl:oneOf rdf:nodeID="values"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>one</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
    ),
)
def test_rdf_mapping_rejects_invalid_detached_data_enumeration(body: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_UNSUPPORTED"


def test_rdf_mapping_rejects_ambiguous_detached_data_enumeration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <rdfs:Datatype rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="left"/>
    <owl:oneOf rdf:nodeID="right"/>
  </rdfs:Datatype>
  <rdf:Description rdf:nodeID="left">
    <rdf:first>left</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first>right</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_CARDINALITY"


@pytest.mark.parametrize(
    ("body", "total_triples"),
    (
        (
            f"""
  <owl:DataRange rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </owl:DataRange>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>one</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
            4,
        ),
        (
            f"""
  <owl:DataRange rdf:nodeID="range">
    <owl:oneOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:DataRange>
""",
            2,
        ),
    ),
)
def test_rdf_mapping_consumes_detached_owl1_data_enumeration(
    body: str,
    total_triples: int,
) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.total_triples == total_triples
    assert document.rdf_mapping_report.consumed_triples == total_triples
    assert document.rdf_mapping_report.unconsumed == ()
    assert len(document.axioms) == 0


def test_rdf_mapping_rejects_named_detached_owl1_data_enumeration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:DataRange rdf:about="urn:range">
    <owl:oneOf rdf:nodeID="values"/>
  </owl:DataRange>
  <rdf:Description rdf:nodeID="values">
    <rdf:first>one</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_INCOMPLETE"


@pytest.mark.parametrize(
    "body",
    (
        f"""
  <owl:DataRange rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="values"/>
  </owl:DataRange>
  <rdf:Description rdf:nodeID="values">
    <rdf:first rdf:resource="urn:value"/>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
""",
        f"""
  <owl:DataRange rdf:nodeID="range">
    <owl:intersectionOf rdf:resource="{RDF_NAMESPACE}nil"/>
  </owl:DataRange>
""",
    ),
)
def test_rdf_mapping_rejects_invalid_detached_owl1_data_enumeration(body: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">{body}</rdf:RDF>
""".encode()

    with pytest.raises(UnsupportedSyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_UNSUPPORTED"


def test_rdf_mapping_rejects_ambiguous_detached_owl1_data_enumeration() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:DataRange rdf:nodeID="range">
    <owl:oneOf rdf:nodeID="left"/>
    <owl:oneOf rdf:nodeID="right"/>
  </owl:DataRange>
  <rdf:Description rdf:nodeID="left">
    <rdf:first>left</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
  <rdf:Description rdf:nodeID="right">
    <rdf:first>right</rdf:first>
    <rdf:rest rdf:resource="{RDF_NAMESPACE}nil"/>
  </rdf:Description>
</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_MAPPING_CARDINALITY"


def test_rdf_mapping_enforces_the_distinct_ontology_annotation_limit() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:o">
    <rdfs:label>label</rdfs:label>
    <rdfs:comment>comment</rdfs:comment>
  </owl:Ontology>
</rdf:RDF>
""".encode()

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_annotations=2),
        ),
    )
    assert len(document.ontology_annotations) == 2

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_annotations=1),
            ),
        )
    assert raised.value.limit == "max_annotations"


def test_rdf_mapping_combines_and_limits_axiom_annotation_reifications() -> None:
    metadata = """\
<owl:annotatedSource rdf:resource="urn:C"/>
<owl:annotatedProperty rdf:resource="http://www.w3.org/2000/01/rdf-schema#subClassOf"/>
<owl:annotatedTarget rdf:resource="urn:D"/>
"""
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C"><rdfs:subClassOf rdf:resource="urn:D"/></owl:Class>
  <owl:Class rdf:about="urn:D"/>
  <owl:Axiom>{metadata}<rdfs:label>label</rdfs:label></owl:Axiom>
  <owl:Axiom>{metadata}<rdfs:comment>comment</rdfs:comment></owl:Axiom>
</rdf:RDF>
""".encode()

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_annotations=2),
        ),
    )
    subclass = next(document.iter_axioms(m.SubClassOf))
    assert len(subclass.annotations) == 2

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_annotations=1),
            ),
        )
    assert raised.value.limit == "max_annotations"


def test_rdf_mapping_claims_annotated_declaration_reification() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C"/>
  <owl:Axiom>
    <owl:annotatedSource rdf:resource="urn:C"/>
    <owl:annotatedProperty rdf:resource="{RDF_NAMESPACE}type"/>
    <owl:annotatedTarget rdf:resource="http://www.w3.org/2002/07/owl#Class"/>
    <rdfs:comment>declared</rdfs:comment>
  </owl:Axiom>
</rdf:RDF>
""".encode()

    document = parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    declaration = next(document.iter_axioms(m.Declaration))
    assert len(declaration.annotations) == 1


def test_rdf_mapping_limits_structural_node_annotations() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:NegativePropertyAssertion>
    <owl:sourceIndividual rdf:resource="urn:i"/>
    <owl:assertionProperty rdf:resource="urn:p"/>
    <owl:targetIndividual rdf:resource="urn:j"/>
    <rdfs:label>label</rdfs:label>
    <rdfs:comment>comment</rdfs:comment>
  </owl:NegativePropertyAssertion>
</rdf:RDF>
""".encode()

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_annotations=2),
        ),
    )
    assertion = next(document.iter_axioms(m.NegativeObjectPropertyAssertion))
    assert len(assertion.annotations) == 2

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_annotations=1),
            ),
        )
    assert raised.value.limit == "max_annotations"


def test_rdf_mapping_limits_nested_annotation_reifications() -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="urn:C"><rdfs:subClassOf rdf:resource="urn:D"/></owl:Class>
  <owl:Class rdf:about="urn:D"/>
  <owl:Axiom rdf:nodeID="axiom">
    <owl:annotatedSource rdf:resource="urn:C"/>
    <owl:annotatedProperty
      rdf:resource="http://www.w3.org/2000/01/rdf-schema#subClassOf"/>
    <owl:annotatedTarget rdf:resource="urn:D"/>
    <rdfs:label>base</rdfs:label>
  </owl:Axiom>
  <owl:Annotation>
    <owl:annotatedSource rdf:nodeID="axiom"/>
    <owl:annotatedProperty rdf:resource="http://www.w3.org/2000/01/rdf-schema#label"/>
    <owl:annotatedTarget>base</owl:annotatedTarget>
    <rdfs:comment>nested</rdfs:comment>
    <rdfs:seeAlso rdf:resource="urn:nested"/>
  </owl:Annotation>
</rdf:RDF>
""".encode()

    document = parse_document(
        source,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_annotations=2),
        ),
    )
    subclass = next(document.iter_axioms(m.SubClassOf))
    assert len(subclass.annotations) == 1
    assert len(next(iter(subclass.annotations)).annotations) == 2

    with pytest.raises(ResourceLimitError) as raised:
        parse_document(
            source,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_annotations=1),
            ),
        )
    assert raised.value.limit == "max_annotations"


@pytest.mark.parametrize(
    "body",
    (
        """
  <owl:Class rdf:about="urn:C"><rdfs:subClassOf rdf:resource="urn:D"/></owl:Class>
  <owl:Axiom>
    <owl:annotatedSource rdf:resource="urn:C"/>
    <owl:annotatedSource rdf:resource="urn:Other"/>
    <owl:annotatedProperty
      rdf:resource="http://www.w3.org/2000/01/rdf-schema#subClassOf"/>
    <owl:annotatedTarget rdf:resource="urn:D"/>
  </owl:Axiom>
""",
        f"""
  <owl:Class rdf:about="urn:C">
    <rdf:type rdf:resource="http://www.w3.org/2000/01/rdf-schema#Class"/>
  </owl:Class>
  <owl:Axiom>
    <owl:annotatedSource rdf:resource="urn:C"/>
    <owl:annotatedProperty rdf:resource="{RDF_NAMESPACE}type"/>
    <owl:annotatedTarget rdf:resource="http://www.w3.org/2000/01/rdf-schema#Class"/>
    <e:note rdf:resource="urn:value"/>
  </owl:Axiom>
""",
        """
  <rdf:Description rdf:about="urn:s"><e:p rdf:resource="urn:o"/></rdf:Description>
  <owl:Annotation>
    <owl:annotatedSource rdf:resource="urn:s"/>
    <owl:annotatedProperty rdf:resource="urn:p"/>
    <owl:annotatedTarget rdf:resource="urn:o"/>
    <e:q rdf:resource="urn:value"/>
  </owl:Annotation>
""",
        """
  <rdf:Description rdf:about="urn:s"><e:p rdf:resource="urn:o"/></rdf:Description>
  <rdf:Description rdf:nodeID="reification">
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Axiom"/>
    <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Annotation"/>
    <owl:annotatedSource rdf:resource="urn:s"/>
    <owl:annotatedProperty rdf:resource="urn:p"/>
    <owl:annotatedTarget rdf:resource="urn:o"/>
    <e:q rdf:resource="urn:value"/>
  </rdf:Description>
""",
    ),
)
def test_rdf_mapping_rejects_malformed_or_unclaimed_reifications(body: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:e="urn:">
{body}</rdf:RDF>
""".encode()

    with pytest.raises(OntologySyntaxError) as raised:
        parse_document(source, format="rdfxml", options=PYTHON_OPTIONS)
    assert raised.value.code == "RDF_AXIOM_REIFICATION"


def test_rdf_mapping_maps_explicitly_enabled_swrl_rule_extensions() -> None:
    with pytest.raises(UnsupportedSyntaxError) as disabled:
        PythonParser().parse(SWRL_RDF_XML, format="rdfxml", options=PYTHON_OPTIONS)
    assert disabled.value.code == "RDF_EXTENSION_DISABLED"

    document = PythonParser().parse(
        SWRL_RDF_XML,
        format="rdfxml",
        options=PYTHON_OPTIONS,
        allow_swrl=True,
    )
    rule = next(iter(document.extension_components))
    assert isinstance(rule, swrl.SWRLRule)
    assert {type(atom) for atom in rule.body} == {
        swrl.ClassAtom,
        swrl.DataRangeAtom,
        swrl.ObjectPropertyAtom,
        swrl.DataPropertyAtom,
        swrl.BuiltInAtom,
        swrl.SameIndividualAtom,
    }
    assert {type(atom) for atom in rule.head} == {swrl.DifferentIndividualsAtom}
    assert len(rule.annotations) == 1
    assert not document.axioms
    assert document.rdf_mapping_report is not None
    assert document.rdf_mapping_report.conformant


def test_turtle_rdf_mapping_uses_the_same_explicit_swrl_gate() -> None:
    source = b"""\
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix swrl: <http://www.w3.org/2003/11/swrl#> .
[] a swrl:Imp ; swrl:body rdf:nil ; swrl:head rdf:nil .
"""

    with pytest.raises(UnsupportedSyntaxError) as disabled:
        PythonParser().parse(source, format="turtle", options=PYTHON_OPTIONS)
    assert disabled.value.code == "RDF_EXTENSION_DISABLED"

    document = PythonParser().parse(
        source,
        format="turtle",
        options=PYTHON_OPTIONS,
        allow_swrl=True,
    )
    rule = next(iter(document.extension_components))
    assert isinstance(rule, swrl.SWRLRule)
    assert not rule.body
    assert not rule.head


@pytest.mark.parametrize(
    ("body", "code"),
    (
        (
            """
  <swrl:Imp>
    <swrl:body rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"/>
  </swrl:Imp>
""",
            "RDF_MAPPING_CARDINALITY",
        ),
        (
            """
  <swrl:Imp>
    <swrl:body rdf:parseType="Collection">
      <swrl:ClassAtom>
        <swrl:classPredicate rdf:resource="urn:C"/>
        <swrl:argument1 rdf:resource="urn:i"/>
        <e:extra rdf:resource="urn:value"/>
      </swrl:ClassAtom>
    </swrl:body>
    <swrl:head rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"/>
  </swrl:Imp>
""",
            "RDF_MAPPING_INCOMPLETE",
        ),
        (
            """
  <swrl:Variable rdf:nodeID="x"/>
  <swrl:Imp>
    <swrl:body rdf:parseType="Collection">
      <swrl:ClassAtom>
        <swrl:classPredicate rdf:resource="urn:C"/>
        <swrl:argument1 rdf:nodeID="x"/>
      </swrl:ClassAtom>
    </swrl:body>
    <swrl:head rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"/>
  </swrl:Imp>
""",
            "RDF_MAPPING_TYPE",
        ),
    ),
)
def test_rdf_mapping_rejects_malformed_swrl_rules(body: str, code: str) -> None:
    source = f"""\
<rdf:RDF xmlns:rdf="{RDF_NAMESPACE}"
         xmlns:swrl="http://www.w3.org/2003/11/swrl#"
         xmlns:e="urn:">
{body}</rdf:RDF>
""".encode()

    with pytest.raises((OntologySyntaxError, UnsupportedSyntaxError)) as raised:
        PythonParser().parse(
            source,
            format="rdfxml",
            options=PYTHON_OPTIONS,
            allow_swrl=True,
        )
    assert raised.value.code == code


def test_rdf_mapping_enforces_canonical_swrl_atom_limit() -> None:
    document = PythonParser().parse(
        SWRL_RDF_XML,
        format="rdfxml",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_rule_atoms=6),
        ),
        allow_swrl=True,
    )
    assert len(next(iter(document.extension_components)).body) == 6

    with pytest.raises(ResourceLimitError) as raised:
        PythonParser().parse(
            SWRL_RDF_XML,
            format="rdfxml",
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_rule_atoms=5),
            ),
            allow_swrl=True,
        )
    assert raised.value.limit == "max_rule_atoms"


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
            34,
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
