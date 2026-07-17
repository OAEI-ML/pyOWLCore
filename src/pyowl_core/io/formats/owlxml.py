"""Secure pure-Python OWL 2 XML Serialization reader and writer."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from typing import Any, NoReturn, cast

import pyowl_core.model as m
from pyowl_core.cancellation import CancellationToken
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.document import OntologyDocument, OntologyID
from pyowl_core.document.document import provisional_anonymous
from pyowl_core.exceptions import OntologySyntaxError, UnsupportedSyntaxError
from pyowl_core.limits import ParseLimits

from .common import ParseContext, ParsedOntology

OWL_NS = "http://www.w3.org/2002/07/owl#"
XML_NS = "http://www.w3.org/XML/1998/namespace"
_FORBIDDEN_XML = re.compile(rb"(?is)<!\s*(?:DOCTYPE|ENTITY)|<\s*(?:xi:)?include\b")
_FORBIDDEN_XML_TEXT = re.compile(r"(?is)<!\s*(?:DOCTYPE|ENTITY)|<\s*(?:xi:)?include\b")
_XML_ENCODING = re.compile(rb"(?i)^\s*<\?xml[^>]*\bencoding\s*=\s*['\"]([^'\"]+)")
_PREFIX_XMLNS = re.compile(rb"\bxmlns(?::([A-Za-z_][\w.-]*))?\s*=\s*(['\"])(.*?)\2", re.DOTALL)


def parse_owlxml(
    data: bytes,
    *,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
) -> ParsedOntology:
    if _has_forbidden_xml(data):
        raise OntologySyntaxError(
            "DTD, entity declarations, and XInclude are forbidden",
            code="XML_FORBIDDEN_CONSTRUCT",
        )
    context = ParseContext(limits, cancellation_token)
    pull: ET.XMLPullParser[ET.Element[str]] = ET.XMLPullParser(events=("start", "end"))
    depth = 0
    root: ET.Element | None = None
    try:
        for offset in range(0, len(data), 64 * 1024):
            context.check()
            pull.feed(data[offset : offset + 64 * 1024])
            for raw_event in pull.read_events():
                event, element = cast(tuple[str, ET.Element], raw_event)
                if event == "start":
                    depth += 1
                    context.depth(depth)
                    root = element if root is None else root
                else:
                    depth -= 1
        pull.close()
    except ET.ParseError as error:
        diagnostic = Diagnostic(
            code="OWLXML_SYNTAX",
            severity=Severity.ERROR,
            message="malformed OWL/XML document",
            details={"rule": "OWL2-XML"},
        )
        raise OntologySyntaxError("malformed OWL/XML document", diagnostic=diagnostic) from error
    if root is None or _namespace(root.tag) != OWL_NS or _local(root.tag) != "Ontology":
        raise OntologySyntaxError(
            "OWL/XML root must be owl:Ontology",
            code="OWLXML_ROOT",
        )
    parser = OWLXMLParser(root, limits, cancellation_token)
    parsed = parser.parse()
    return ParsedOntology(
        parsed.ontology_id,
        parsed.imports,
        parsed.annotations,
        parsed.axioms,
        parsed.extensions,
        tuple(sorted(_xml_prefixes(data).items())),
        parsed.occurrences,
        decoded_codepoint_length=_decoded_xml_length(data),
    )


class OWLXMLParser:
    __slots__ = ("context", "prefixes", "root")

    def __init__(
        self,
        root: ET.Element,
        limits: ParseLimits,
        cancellation_token: CancellationToken | None,
    ) -> None:
        self.root = root
        self.context = ParseContext(limits, cancellation_token)
        self.prefixes: dict[str, str] = {
            "owl:": OWL_NS,
            "rdf:": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "rdfs:": "http://www.w3.org/2000/01/rdf-schema#",
            "xsd:": "http://www.w3.org/2001/XMLSchema#",
        }

    def parse(self) -> ParsedOntology:
        ontology_iri = self._optional_attribute_iri(self.root, "ontologyIRI")
        version_iri = self._optional_attribute_iri(self.root, "versionIRI")
        imports: list[m.IRI] = []
        annotations: list[m.Annotation] = []
        axioms: list[m.AxiomNode] = []
        occurrences: list[tuple[m.StructuralNode, None]] = []
        children = list(self.root)
        for child in children:
            self.context.check()
            name = self._name(child)
            if name == "Prefix":
                prefix = child.get("name")
                iri = child.get("IRI")
                if prefix is None or iri is None:
                    self._syntax("Prefix requires name and IRI attributes")
                self.prefixes[prefix] = m.IRI(iri).value
                self.context.limits.enforce("max_prefixes", len(self.prefixes))
            elif name == "Import":
                imports.append(self._text_iri(child))
            elif name == "Annotation":
                annotation = self._annotation(child)
                annotations.append(annotation)
                occurrences.append((annotation, None))
            else:
                axiom = self._axiom(child)
                axioms.append(axiom)
                occurrences.append((axiom, None))
                self.context.limits.enforce("max_axioms", len(axioms))
        return ParsedOntology(
            OntologyID(ontology_iri, version_iri),
            tuple(imports),
            tuple(annotations),
            tuple(axioms),
            prefixes=tuple(sorted(self.prefixes.items())),
            occurrences=tuple(occurrences),
        )

    def _axiom(self, element: ET.Element) -> m.AxiomNode:
        name = self._name(element)
        children = list(element)
        annotations, children = self._leading_annotations(children)
        if name == "Declaration":
            self._arity(name, children, 1)
            return m.Declaration(self._entity(children[0]), annotations)
        if name == "SubClassOf":
            self._arity(name, children, 2)
            return m.SubClassOf(
                self._class_expression(children[0]),
                self._class_expression(children[1]),
                annotations,
            )
        if name in {"EquivalentClasses", "DisjointClasses"}:
            return cast(
                m.AxiomNode,
                getattr(m, name)(
                    m.CanonicalSet(map(self._class_expression, children)), annotations
                ),
            )
        if name == "DisjointUnion":
            if len(children) < 3:
                self._syntax("DisjointUnion requires a class and at least two expressions")
            return m.DisjointUnion(
                self._class(children[0]),
                m.CanonicalSet(map(self._class_expression, children[1:])),
                annotations,
            )
        if name == "SubObjectPropertyOf":
            self._arity(name, children, 2)
            return m.SubObjectPropertyOf(
                self._sub_object_property(children[0]),
                self._object_property(children[1]),
                annotations,
            )
        if name in {"EquivalentObjectProperties", "DisjointObjectProperties"}:
            return cast(
                m.AxiomNode,
                getattr(m, name)(m.CanonicalSet(map(self._object_property, children)), annotations),
            )
        if name == "InverseObjectProperties":
            self._arity(name, children, 2)
            return m.InverseObjectProperties(
                self._object_property(children[0]), self._object_property(children[1]), annotations
            )
        if name in {"ObjectPropertyDomain", "ObjectPropertyRange"}:
            self._arity(name, children, 2)
            return cast(
                m.AxiomNode,
                getattr(m, name)(
                    self._object_property(children[0]),
                    self._class_expression(children[1]),
                    annotations,
                ),
            )
        if name in _OBJECT_CHARACTERISTICS:
            self._arity(name, children, 1)
            return cast(
                m.AxiomNode,
                getattr(m, name)(self._object_property(children[0]), annotations),
            )
        if name == "SubDataPropertyOf":
            self._arity(name, children, 2)
            return m.SubDataPropertyOf(
                self._data_property(children[0]), self._data_property(children[1]), annotations
            )
        if name in {"EquivalentDataProperties", "DisjointDataProperties"}:
            return cast(
                m.AxiomNode,
                getattr(m, name)(m.CanonicalSet(map(self._data_property, children)), annotations),
            )
        if name == "DataPropertyDomain":
            self._arity(name, children, 2)
            return m.DataPropertyDomain(
                self._data_property(children[0]), self._class_expression(children[1]), annotations
            )
        if name == "DataPropertyRange":
            self._arity(name, children, 2)
            return m.DataPropertyRange(
                self._data_property(children[0]), self._data_range(children[1]), annotations
            )
        if name == "FunctionalDataProperty":
            self._arity(name, children, 1)
            return m.FunctionalDataProperty(self._data_property(children[0]), annotations)
        if name == "DatatypeDefinition":
            self._arity(name, children, 2)
            return m.DatatypeDefinition(
                self._datatype(children[0]), self._data_range(children[1]), annotations
            )
        if name == "HasKey":
            if len(children) < 2:
                self._syntax("HasKey requires a class expression and a property")
            expression = self._class_expression(children[0])
            object_properties = tuple(
                self._object_property(item)
                for item in children[1:]
                if self._name(item) in {"ObjectProperty", "ObjectInverseOf"}
            )
            data_properties = tuple(
                self._data_property(item)
                for item in children[1:]
                if self._name(item) == "DataProperty"
            )
            if len(object_properties) + len(data_properties) != len(children) - 1:
                self._syntax("HasKey contains an invalid property expression")
            return m.HasKey(
                expression,
                m.CanonicalSet(object_properties),
                m.CanonicalSet(data_properties),
                annotations,
            )
        if name in {"SameIndividual", "DifferentIndividuals"}:
            return cast(
                m.AxiomNode,
                getattr(m, name)(m.CanonicalSet(map(self._individual, children)), annotations),
            )
        if name == "ClassAssertion":
            self._arity(name, children, 2)
            return m.ClassAssertion(
                self._class_expression(children[0]), self._individual(children[1]), annotations
            )
        if name in {"ObjectPropertyAssertion", "NegativeObjectPropertyAssertion"}:
            self._arity(name, children, 3)
            return cast(
                m.AxiomNode,
                getattr(m, name)(
                    self._object_property(children[0]),
                    self._individual(children[1]),
                    self._individual(children[2]),
                    annotations,
                ),
            )
        if name in {"DataPropertyAssertion", "NegativeDataPropertyAssertion"}:
            self._arity(name, children, 3)
            return cast(
                m.AxiomNode,
                getattr(m, name)(
                    self._data_property(children[0]),
                    self._individual(children[1]),
                    self._literal(children[2]),
                    annotations,
                ),
            )
        if name == "AnnotationAssertion":
            self._arity(name, children, 3)
            return m.AnnotationAssertion(
                self._annotation_property(children[0]),
                self._annotation_subject(children[1]),
                self._annotation_value(children[2]),
                annotations,
            )
        if name == "SubAnnotationPropertyOf":
            self._arity(name, children, 2)
            return m.SubAnnotationPropertyOf(
                self._annotation_property(children[0]),
                self._annotation_property(children[1]),
                annotations,
            )
        if name in {"AnnotationPropertyDomain", "AnnotationPropertyRange"}:
            self._arity(name, children, 2)
            return cast(
                m.AxiomNode,
                getattr(m, name)(
                    self._annotation_property(children[0]),
                    self._iri_element(children[1]),
                    annotations,
                ),
            )
        self._syntax(f"unknown OWL/XML axiom element {name!r}")

    def _class_expression(self, element: ET.Element) -> m.ClassExpression:
        name = self._name(element)
        children = list(element)
        if name == "Class":
            return self._class(element)
        if name in {"ObjectIntersectionOf", "ObjectUnionOf"}:
            return cast(
                m.ClassExpression,
                getattr(m, name)(m.CanonicalSet(map(self._class_expression, children))),
            )
        if name == "ObjectComplementOf":
            self._arity(name, children, 1)
            return m.ObjectComplementOf(self._class_expression(children[0]))
        if name == "ObjectOneOf":
            return m.ObjectOneOf(m.CanonicalSet(map(self._individual, children)))
        if name in {"ObjectSomeValuesFrom", "ObjectAllValuesFrom"}:
            self._arity(name, children, 2)
            return cast(
                m.ClassExpression,
                getattr(m, name)(
                    self._object_property(children[0]), self._class_expression(children[1])
                ),
            )
        if name == "ObjectHasValue":
            self._arity(name, children, 2)
            return m.ObjectHasValue(
                self._object_property(children[0]), self._individual(children[1])
            )
        if name == "ObjectHasSelf":
            self._arity(name, children, 1)
            return m.ObjectHasSelf(self._object_property(children[0]))
        if name in _OBJECT_CARDINALITIES:
            cardinality = self._cardinality(element)
            if len(children) not in {1, 2}:
                self._syntax(f"{name} requires a property and optional filler")
            class_filler = (
                m.OWL_THING if len(children) == 1 else self._class_expression(children[1])
            )
            return cast(
                m.ClassExpression,
                getattr(m, name)(cardinality, self._object_property(children[0]), class_filler),
            )
        if name in {"DataSomeValuesFrom", "DataAllValuesFrom"}:
            if len(children) < 2:
                self._syntax(f"{name} requires properties and a data range")
            return cast(
                m.ClassExpression,
                getattr(m, name)(
                    tuple(map(self._data_property, children[:-1])),
                    self._data_range(children[-1]),
                ),
            )
        if name == "DataHasValue":
            self._arity(name, children, 2)
            return m.DataHasValue(self._data_property(children[0]), self._literal(children[1]))
        if name in _DATA_CARDINALITIES:
            cardinality = self._cardinality(element)
            if len(children) not in {1, 2}:
                self._syntax(f"{name} requires a property and optional filler")
            data_filler = m.RDFS_LITERAL if len(children) == 1 else self._data_range(children[1])
            return cast(
                m.ClassExpression,
                getattr(m, name)(cardinality, self._data_property(children[0]), data_filler),
            )
        self._syntax(f"unknown OWL/XML class expression {name!r}")

    def _data_range(self, element: ET.Element) -> m.DataRange:
        name = self._name(element)
        children = list(element)
        if name == "Datatype":
            return self._datatype(element)
        if name in {"DataIntersectionOf", "DataUnionOf"}:
            return cast(
                m.DataRange,
                getattr(m, name)(m.CanonicalSet(map(self._data_range, children))),
            )
        if name == "DataComplementOf":
            self._arity(name, children, 1)
            return m.DataComplementOf(self._data_range(children[0]))
        if name == "DataOneOf":
            return m.DataOneOf(m.CanonicalSet(map(self._literal, children)))
        if name == "DatatypeRestriction":
            if len(children) < 2:
                self._syntax("DatatypeRestriction requires a datatype and facet restriction")
            return m.DatatypeRestriction(
                self._datatype(children[0]),
                m.CanonicalSet(map(self._facet, children[1:])),
            )
        self._syntax(f"unknown OWL/XML data range {name!r}")

    def _facet(self, element: ET.Element) -> m.FacetRestriction:
        if self._name(element) != "FacetRestriction":
            self._syntax("expected FacetRestriction")
        facet = element.get("facet")
        children = list(element)
        if facet is None or len(children) != 1:
            self._syntax("FacetRestriction requires facet and one Literal")
        return m.FacetRestriction(m.IRI(facet), self._literal(children[0]))

    def _sub_object_property(self, element: ET.Element) -> m.SubObjectPropertyExpression:
        if self._name(element) == "ObjectPropertyChain":
            return m.ObjectPropertyChain(tuple(map(self._object_property, list(element))))
        return self._object_property(element)

    def _object_property(self, element: ET.Element) -> m.ObjectPropertyExpression:
        name = self._name(element)
        if name == "ObjectProperty":
            return m.ObjectProperty(self._entity_iri(element))
        if name == "ObjectInverseOf":
            children = list(element)
            self._arity(name, children, 1)
            prop = self._object_property(children[0])
            if not isinstance(prop, m.ObjectProperty):
                self._syntax("ObjectInverseOf cannot directly contain another inverse")
            return m.ObjectInverseOf(prop)
        self._syntax("expected an object property expression")

    def _annotation(self, element: ET.Element) -> m.Annotation:
        children = list(element)
        annotations, children = self._leading_annotations(children)
        self._arity("Annotation", children, 2)
        return m.Annotation(
            self._annotation_property(children[0]), self._annotation_value(children[1]), annotations
        )

    def _leading_annotations(
        self, children: list[ET.Element]
    ) -> tuple[m.CanonicalSet[m.Annotation], list[ET.Element]]:
        annotations: list[m.Annotation] = []
        index = 0
        while index < len(children) and self._name(children[index]) == "Annotation":
            annotations.append(self._annotation(children[index]))
            index += 1
            self.context.limits.enforce("max_annotations", len(annotations))
        return m.CanonicalSet(annotations), children[index:]

    def _entity(self, element: ET.Element) -> m.Entity:
        constructors: dict[str, Callable[[m.IRI], m.Entity]] = {
            "Class": m.Class,
            "Datatype": m.Datatype,
            "ObjectProperty": m.ObjectProperty,
            "DataProperty": m.DataProperty,
            "AnnotationProperty": m.AnnotationProperty,
            "NamedIndividual": m.NamedIndividual,
        }
        try:
            constructor = constructors[self._name(element)]
        except KeyError:
            self._syntax("expected an OWL entity element")
        return constructor(self._entity_iri(element))

    def _class(self, element: ET.Element) -> m.Class:
        if self._name(element) != "Class":
            self._syntax("expected Class")
        return m.Class(self._entity_iri(element))

    def _datatype(self, element: ET.Element) -> m.Datatype:
        if self._name(element) != "Datatype":
            self._syntax("expected Datatype")
        return m.Datatype(self._entity_iri(element))

    def _data_property(self, element: ET.Element) -> m.DataProperty:
        if self._name(element) != "DataProperty":
            self._syntax("expected DataProperty")
        return m.DataProperty(self._entity_iri(element))

    def _annotation_property(self, element: ET.Element) -> m.AnnotationProperty:
        if self._name(element) != "AnnotationProperty":
            self._syntax("expected AnnotationProperty")
        return m.AnnotationProperty(self._entity_iri(element))

    def _individual(self, element: ET.Element) -> m.Individual:
        name = self._name(element)
        if name == "NamedIndividual":
            return m.NamedIndividual(self._entity_iri(element))
        if name == "AnonymousIndividual":
            node_id = element.get("nodeID")
            if node_id is None or not node_id:
                self._syntax("AnonymousIndividual requires nodeID")
            return provisional_anonymous(node_id)
        self._syntax("expected an individual")

    def _literal(self, element: ET.Element) -> m.Literal:
        if self._name(element) != "Literal":
            self._syntax("expected Literal")
        lexical = element.text or ""
        self.context.limits.enforce("max_literal_bytes", len(lexical.encode("utf-8")))
        language = element.get("lang") or element.get(f"{{{XML_NS}}}lang")
        datatype_value = element.get("datatypeIRI")
        if language is not None:
            return m.Literal(lexical, m.RDF_PLAIN_LITERAL, language)
        if datatype_value is None:
            return m.Literal(lexical, m.RDF_PLAIN_LITERAL)
        return m.Literal(lexical, m.Datatype(m.IRI(datatype_value)))

    def _annotation_subject(self, element: ET.Element) -> m.AnnotationSubject:
        if self._name(element) == "AnonymousIndividual":
            return cast(m.AnonymousIndividual, self._individual(element))
        return self._iri_element(element)

    def _annotation_value(self, element: ET.Element) -> m.AnnotationValue:
        name = self._name(element)
        if name == "Literal":
            return self._literal(element)
        if name == "AnonymousIndividual":
            return cast(m.AnonymousIndividual, self._individual(element))
        return self._iri_element(element)

    def _iri_element(self, element: ET.Element) -> m.IRI:
        name = self._name(element)
        text = (element.text or "").strip()
        if name == "IRI":
            return m.IRI(text)
        if name == "AbbreviatedIRI":
            return self._expand(text)
        self._syntax("expected IRI or AbbreviatedIRI element")

    def _entity_iri(self, element: ET.Element) -> m.IRI:
        direct = element.get("IRI")
        abbreviated = element.get("abbreviatedIRI")
        if (direct is None) == (abbreviated is None):
            self._syntax("entity element requires exactly one of IRI or abbreviatedIRI")
        return m.IRI(direct) if direct is not None else self._expand(abbreviated or "")

    def _optional_attribute_iri(self, element: ET.Element, name: str) -> m.IRI | None:
        value = element.get(name)
        return None if value is None else m.IRI(value)

    def _text_iri(self, element: ET.Element) -> m.IRI:
        return m.IRI((element.text or "").strip())

    def _expand(self, value: str) -> m.IRI:
        if ":" not in value:
            self._syntax("abbreviatedIRI requires a prefix")
        prefix, local = value.split(":", 1)
        key = prefix + ":"
        try:
            return m.IRI(self.prefixes[key] + local)
        except KeyError:
            self._syntax(f"undefined OWL/XML prefix {key!r}")

    def _cardinality(self, element: ET.Element) -> int:
        value = element.get("cardinality")
        if value is None or not value.isdigit():
            self._syntax("cardinality attribute must be a nonnegative integer")
        return int(value)

    def _name(self, element: ET.Element) -> str:
        if _namespace(element.tag) != OWL_NS:
            self._syntax("all OWL/XML structural elements must use the OWL namespace")
        return _local(element.tag)

    def _arity(self, name: str, children: list[ET.Element], expected: int) -> None:
        if len(children) != expected:
            self._syntax(f"{name} requires exactly {expected} child element(s)")

    @staticmethod
    def _syntax(message: str) -> NoReturn:
        raise OntologySyntaxError(message, code="OWLXML_SYNTAX")


_OBJECT_CHARACTERISTICS = frozenset(
    {
        "FunctionalObjectProperty",
        "InverseFunctionalObjectProperty",
        "ReflexiveObjectProperty",
        "IrreflexiveObjectProperty",
        "SymmetricObjectProperty",
        "AsymmetricObjectProperty",
        "TransitiveObjectProperty",
    }
)
_OBJECT_CARDINALITIES = frozenset(
    {"ObjectMinCardinality", "ObjectMaxCardinality", "ObjectExactCardinality"}
)
_DATA_CARDINALITIES = frozenset(
    {"DataMinCardinality", "DataMaxCardinality", "DataExactCardinality"}
)


def render_owlxml(document: OntologyDocument) -> bytes:
    if document.extension_components:
        raise UnsupportedSyntaxError(
            "OWL/XML has no standard SWRL structural extension serialization",
            code="OWLXML_EXTENSION_UNSUPPORTED",
        )
    ET.register_namespace("", OWL_NS)
    attributes: dict[str, str] = {}
    if document.ontology_id.ontology_iri is not None:
        attributes["ontologyIRI"] = document.ontology_id.ontology_iri.value
    if document.ontology_id.version_iri is not None:
        attributes["versionIRI"] = document.ontology_id.version_iri.value
    root = ET.Element(_tag("Ontology"), attributes)
    for iri in document.direct_imports:
        child = ET.SubElement(root, _tag("Import"))
        child.text = iri.value
    for annotation in document.ontology_annotations:
        root.append(_xml_node(annotation))
    for axiom in document.axioms:
        root.append(_xml_node(axiom))
    ET.indent(root, space="  ")
    return cast(
        bytes,
        ET.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        ),
    )


def _xml_node(value: object) -> ET.Element:
    if isinstance(value, m.Entity):
        return ET.Element(_tag(type(value).__name__), {"IRI": value.iri.value})
    if isinstance(value, m.AnonymousIndividual):
        return ET.Element(_tag("AnonymousIndividual"), {"nodeID": "b" + value.local_key.hex()})
    if isinstance(value, m.Literal):
        literal_attributes: dict[str, str] = {}
        if value.language is not None:
            literal_attributes["lang"] = value.language
        elif value.datatype != m.RDF_PLAIN_LITERAL:
            literal_attributes["datatypeIRI"] = value.datatype.iri.value
        element = ET.Element(_tag("Literal"), literal_attributes)
        element.text = value.lexical_form
        return element
    if isinstance(value, m.IRI):
        element = ET.Element(_tag("IRI"))
        element.text = value.value
        return element
    if isinstance(value, m.Annotation):
        element = ET.Element(_tag("Annotation"))
        _append_many(element, value.annotations)
        element.append(_xml_node(value.property))
        element.append(_xml_node(value.value))
        return element
    if isinstance(value, m.Declaration):
        element = ET.Element(_tag("Declaration"))
        _append_many(element, value.annotations)
        element.append(_xml_node(value.entity))
        return element
    if isinstance(value, m.FacetRestriction):
        element = ET.Element(_tag("FacetRestriction"), {"facet": value.facet.value})
        element.append(_xml_node(value.value))
        return element
    if isinstance(value, m.StructuralNode):
        name = type(value).__name__
        node = cast(Any, value)
        node_attributes: dict[str, str] = {}
        if name in _OBJECT_CARDINALITIES | _DATA_CARDINALITIES:
            node_attributes["cardinality"] = str(node.cardinality)
        element = ET.Element(_tag(name), node_attributes)
        annotations = getattr(node, "annotations", ())
        _append_many(element, annotations)
        if isinstance(value, m.HasKey):
            element.append(_xml_node(value.class_expression))
            _append_many(element, value.object_properties)
            _append_many(element, value.data_properties)
            return element
        spec = m.constructor_spec(value)
        for field in spec.fields:
            if field in {"annotations", "cardinality"}:
                continue
            item = getattr(node, field)
            if isinstance(item, (m.CanonicalSet, tuple)):
                _append_many(element, item)
            else:
                element.append(_xml_node(item))
        return element
    raise TypeError(f"cannot render {type(value).__name__} in OWL/XML")


def _append_many(parent: ET.Element, values: Iterable[object]) -> None:
    for value in values:
        parent.append(_xml_node(value))


def _tag(local: str) -> str:
    return f"{{{OWL_NS}}}{local}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _xml_prefixes(data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in _PREFIX_XMLNS.finditer(data[: min(len(data), 256 * 1024)]):
        prefix = b"" if match.group(1) is None else match.group(1)
        try:
            result[prefix.decode("ascii")] = match.group(3).decode("utf-8")
        except UnicodeDecodeError:
            continue
    return result


def _decoded_xml_length(data: bytes) -> int:
    wide_encoding = _wide_xml_encoding(data)
    if wide_encoding is not None:
        encoding = wide_encoding
        match = None
    elif data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
        match = None
    else:
        encoding = "utf-8"
        match = _XML_ENCODING.match(data[:512])
    try:
        if match is not None:
            encoding = match.group(1).decode("ascii", "strict")
        return len(data.decode(encoding))
    except (LookupError, UnicodeDecodeError) as error:
        raise OntologySyntaxError(
            "invalid or unsupported XML encoding", code="XML_ENCODING"
        ) from error


def _has_forbidden_xml(data: bytes) -> bool:
    if _FORBIDDEN_XML.search(data):
        return True
    encoding = _wide_xml_encoding(data)
    if encoding is None:
        return False
    try:
        return _FORBIDDEN_XML_TEXT.search(data.decode(encoding)) is not None
    except UnicodeDecodeError:
        return False


def _wide_xml_encoding(data: bytes) -> str | None:
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if data.startswith(b"\x00\x00\x00<"):
        return "utf-32-be"
    if data.startswith(b"<\x00\x00\x00"):
        return "utf-32-le"
    if data.startswith(b"\x00<"):
        return "utf-16-be"
    if data.startswith(b"<\x00"):
        return "utf-16-le"
    return None


__all__ = ["OWL_NS", "OWLXMLParser", "parse_owlxml", "render_owlxml"]
