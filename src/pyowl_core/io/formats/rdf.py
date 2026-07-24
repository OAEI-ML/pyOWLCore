"""Internal RDF graph values and normative OWL 2 structural mapping."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, NoReturn, TypeAlias, cast

import pyowl_core.model as m
from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import OntologyDocument, OntologyID
from pyowl_core.document.document import provisional_anonymous
from pyowl_core.document.provenance import RDFMappingReport, RDFTripleEvidence
from pyowl_core.exceptions import InvalidLiteralError, OntologySyntaxError, UnsupportedSyntaxError
from pyowl_core.limits import ParseLimits
from pyowl_core.model.swrl import (
    Atom,
    BuiltInAtom,
    ClassAtom,
    DataArgument,
    DataPropertyAtom,
    DataRangeAtom,
    DifferentIndividualsAtom,
    IndividualArgument,
    ObjectPropertyAtom,
    SameIndividualAtom,
    SWRLRule,
    Variable,
)

from .common import ParseContext, ParsedOntology

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
XSD = "http://www.w3.org/2001/XMLSchema#"
SWRL = "http://www.w3.org/2003/11/swrl#"


@dataclass(frozen=True, slots=True, order=True)
class RDFIRI:
    value: str


@dataclass(frozen=True, slots=True, order=True)
class RDFBlank:
    label: str


@dataclass(frozen=True, slots=True, order=True)
class RDFLiteral:
    lexical: str
    datatype: str | None = None
    language: str | None = None


RDFResource: TypeAlias = RDFIRI | RDFBlank
RDFTerm: TypeAlias = RDFIRI | RDFBlank | RDFLiteral


@dataclass(frozen=True, slots=True)
class Triple:
    subject: RDFResource
    predicate: RDFIRI
    object: RDFTerm

    def key(self) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str, str]]:
        return _resource_key(self.subject), _resource_key(self.predicate), _term_key(self.object)


class RDFGraph:
    __slots__ = ("_objects", "_predicates", "_subjects", "_triple_set", "_triples")

    def __init__(self, triples: Iterable[Triple] = ()) -> None:
        unique = set(triples)
        if not all(isinstance(item, Triple) for item in unique):
            raise TypeError("RDFGraph requires Triple values")
        self._triples = tuple(sorted(unique, key=Triple.key))
        self._triple_set = frozenset(self._triples)
        objects: dict[tuple[RDFResource, RDFIRI], list[RDFTerm]] = {}
        subjects: dict[RDFResource, list[Triple]] = {}
        predicates: dict[RDFIRI, list[Triple]] = {}
        for triple in self._triples:
            objects.setdefault((triple.subject, triple.predicate), []).append(triple.object)
            subjects.setdefault(triple.subject, []).append(triple)
            predicates.setdefault(triple.predicate, []).append(triple)
        self._objects = {key: tuple(values) for key, values in objects.items()}
        self._subjects = {key: tuple(values) for key, values in subjects.items()}
        self._predicates = {key: tuple(values) for key, values in predicates.items()}

    @property
    def triples(self) -> tuple[Triple, ...]:
        return self._triples

    def objects(self, subject: RDFResource, predicate: str | RDFIRI) -> tuple[RDFTerm, ...]:
        iri = predicate if isinstance(predicate, RDFIRI) else RDFIRI(predicate)
        return self._objects.get((subject, iri), ())

    def contains(self, triple: Triple) -> bool:
        return triple in self._triple_set

    def one(
        self, subject: RDFResource, predicate: str | RDFIRI, *, required: bool = False
    ) -> RDFTerm | None:
        values = self.objects(subject, predicate)
        if len(values) > 1:
            raise OntologySyntaxError(
                "RDF mapping predicate has multiple values",
                code="RDF_MAPPING_CARDINALITY",
            )
        if required and not values:
            raise OntologySyntaxError(
                "RDF mapping predicate is missing",
                code="RDF_MAPPING_CARDINALITY",
            )
        return values[0] if values else None

    def find(
        self,
        *,
        subject: RDFResource | None = None,
        predicate: str | RDFIRI | None = None,
        object: RDFTerm | None = None,
    ) -> tuple[Triple, ...]:
        predicate_iri = RDFIRI(predicate) if isinstance(predicate, str) else predicate
        source = (
            self._subjects.get(subject, ())
            if subject is not None
            else self._predicates.get(predicate_iri, ())
            if predicate_iri is not None
            else self._triples
        )
        return tuple(
            item
            for item in source
            if (subject is None or item.subject == subject)
            and (predicate_iri is None or item.predicate == predicate_iri)
            and (object is None or item.object == object)
        )


class RDFMapper:
    __slots__ = (
        "allow_swrl",
        "annotation_annotations",
        "annotation_dependencies",
        "annotation_kinds",
        "axiom_annotations",
        "blank_roles",
        "consumed",
        "context",
        "data_kinds",
        "document_iri",
        "graph",
        "kinds",
        "list_nodes",
        "object_kinds",
        "stack",
        "unclaimed_axiom_reifications",
        "unclaimed_nested_reifications",
    )

    def __init__(
        self,
        graph: RDFGraph,
        *,
        limits: ParseLimits,
        document_iri: m.IRI | None,
        cancellation_token: CancellationToken | None = None,
        allow_swrl: bool = False,
    ) -> None:
        self.graph = graph
        self.context = ParseContext(limits, cancellation_token)
        self.document_iri = document_iri
        self.allow_swrl = allow_swrl
        self.consumed: set[Triple] = set()
        self.kinds: dict[str, set[m.EntityKind]] = {}
        self.object_kinds: set[str] = set(_BUILTIN_OBJECT_PROPERTIES)
        self.data_kinds: set[str] = set(_BUILTIN_DATA_PROPERTIES)
        self.annotation_kinds: set[str] = set(_BUILTIN_ANNOTATION_PROPERTIES)
        self.annotation_annotations: dict[Triple, m.CanonicalSet[m.Annotation]] = {}
        self.annotation_dependencies: dict[Triple, set[Triple]] = {}
        self.axiom_annotations: dict[Triple, m.CanonicalSet[m.Annotation]] = {}
        self.blank_roles: dict[str, str] = {}
        self.list_nodes: dict[RDFBlank, RDFBlank] = {}
        self.stack: set[tuple[str, RDFTerm]] = set()
        self.unclaimed_axiom_reifications: set[Triple] = set()
        self.unclaimed_nested_reifications: set[Triple] = set()

    def map(self, *, allow_partial: bool = False) -> ParsedOntology:
        self._scan_entity_kinds()
        self._consume_owl1_redundant_types()
        self._collect_axiom_annotations()
        ontology_id, imports, ontology_annotations, ontology_node = self._header()
        axioms: list[m.AxiomNode] = []
        extensions: list[m.StructuralNode] = []
        occurrences: list[tuple[m.StructuralNode, None]] = []
        for axiom in self._declarations():
            axioms.append(axiom)
            occurrences.append((axiom, None))
        for extension in self._swrl_rules():
            extensions.append(extension)
            occurrences.append((extension, None))
        for axiom in self._special_axioms():
            axioms.append(axiom)
            occurrences.append((axiom, None))
        for axiom in self._equivalence_components():
            axioms.append(axiom)
            occurrences.append((axiom, None))
        for triple in self.graph.triples:
            self.context.check()
            if triple in self.consumed:
                continue
            simple_axiom = self._simple_axiom(triple, ontology_node)
            if simple_axiom is not None:
                axioms.append(simple_axiom)
                occurrences.append((simple_axiom, None))
        self._consume_detached_inverse_property_expressions()
        self._consume_detached_empty_class_booleans()
        self._consume_detached_named_class_booleans()
        self._consume_detached_class_complements()
        self._consume_detached_object_enumerations()
        self._consume_detached_named_data_booleans()
        self._consume_detached_datatype_restrictions()
        self._consume_detached_data_complements()
        self._consume_detached_data_enumerations()
        self._consume_detached_owl1_data_enumerations()
        if self.unclaimed_axiom_reifications or self.unclaimed_nested_reifications:
            raise OntologySyntaxError(
                "RDF reification targets an unsupported axiom or annotation mapping",
                code="RDF_AXIOM_REIFICATION",
            )
        unconsumed = tuple(item for item in self.graph.triples if item not in self.consumed)
        report = RDFMappingReport(
            conformant=not unconsumed,
            consumed_triples=len(self.consumed),
            total_triples=len(self.graph.triples),
            unconsumed=tuple(
                _evidence(item) for item in unconsumed[: self.context.limits.max_diagnostics]
            ),
            rule_ids=("OWL2-RDF-REVERSE",) if unconsumed else (),
        )
        if unconsumed and not allow_partial:
            examples = "; ".join(
                f"{_term_text(item.subject)} {_term_text(item.predicate)} {_term_text(item.object)}"
                for item in unconsumed[:3]
            )
            raise UnsupportedSyntaxError(
                f"RDF graph is not completely mappable to OWL 2 structure; "
                f"{len(unconsumed)} unconsumed triple(s): {examples}",
                code="RDF_MAPPING_INCOMPLETE",
            )
        parsed = ParsedOntology(
            ontology_id,
            imports,
            ontology_annotations,
            tuple(axioms),
            extensions=tuple(extensions),
            occurrences=tuple(occurrences),
            rdf_mapping_report=report,
        )
        axiom_rows = {
            m.canonical_bytes(root, limits=self.context.limits) for root in parsed.axioms
        }
        self.context.limits.enforce("max_axioms", len(axiom_rows))
        annotation_rows = {
            m.canonical_bytes(root, limits=self.context.limits) for root in parsed.annotations
        }
        self.context.limits.enforce("max_annotations", len(annotation_rows))
        for root in parsed.extensions:
            m.canonical_bytes(root, limits=self.context.limits)
        return parsed

    def _scan_entity_kinds(self) -> None:
        mapping = {
            OWL + "Class": m.EntityKind.CLASS,
            RDFS + "Datatype": m.EntityKind.DATATYPE,
            OWL + "ObjectProperty": m.EntityKind.OBJECT_PROPERTY,
            OWL + "DatatypeProperty": m.EntityKind.DATA_PROPERTY,
            OWL + "AnnotationProperty": m.EntityKind.ANNOTATION_PROPERTY,
            OWL + "OntologyProperty": m.EntityKind.ANNOTATION_PROPERTY,
            OWL + "NamedIndividual": m.EntityKind.NAMED_INDIVIDUAL,
        }
        inferred = {
            OWL + "InverseFunctionalProperty": m.EntityKind.OBJECT_PROPERTY,
            OWL + "SymmetricProperty": m.EntityKind.OBJECT_PROPERTY,
            OWL + "TransitiveProperty": m.EntityKind.OBJECT_PROPERTY,
        }
        for triple in self.graph.find(predicate=RDF + "type"):
            if not isinstance(triple.subject, RDFIRI) or not isinstance(triple.object, RDFIRI):
                continue
            kind = mapping.get(triple.object.value) or inferred.get(triple.object.value)
            if kind is None:
                continue
            self.kinds.setdefault(triple.subject.value, set()).add(kind)
            if kind is m.EntityKind.OBJECT_PROPERTY:
                self.object_kinds.add(triple.subject.value)
            elif kind is m.EntityKind.DATA_PROPERTY:
                self.data_kinds.add(triple.subject.value)
            elif kind is m.EntityKind.ANNOTATION_PROPERTY:
                self.annotation_kinds.add(triple.subject.value)

    def _consume_owl1_redundant_types(self) -> None:
        compatible_types = {
            RDFS + "Class": {
                OWL + "Ontology",
                OWL + "Class",
                RDFS + "Datatype",
                OWL + "DataRange",
                OWL + "Restriction",
            },
            RDF + "Property": {
                OWL + "ObjectProperty",
                OWL + "FunctionalProperty",
                OWL + "InverseFunctionalProperty",
                OWL + "SymmetricProperty",
                OWL + "TransitiveProperty",
                OWL + "DatatypeProperty",
                OWL + "AnnotationProperty",
                OWL + "OntologyProperty",
            },
            OWL + "Class": {OWL + "Restriction"},
        }
        for triple in self.graph.find(predicate=RDF + "type"):
            if not isinstance(triple.object, RDFIRI):
                continue
            expected = compatible_types.get(triple.object.value)
            if expected is None:
                continue
            if any(
                self.graph.contains(
                    Triple(triple.subject, RDFIRI(RDF + "type"), RDFIRI(candidate))
                )
                for candidate in expected
            ):
                self._consume(triple)

    def _consume_detached_inverse_property_expressions(self) -> None:
        for triple in self.graph.find(predicate=OWL + "inverseOf"):
            self.context.check()
            if (
                triple in self.consumed
                or not isinstance(triple.subject, RDFBlank)
                or not isinstance(triple.object, RDFIRI)
                or triple.object.value not in self.object_kinds
            ):
                continue
            self._object_property(triple.subject)

    def _consume_detached_empty_class_booleans(self) -> None:
        for predicate in (OWL + "intersectionOf", OWL + "unionOf"):
            for triple in self.graph.find(predicate=predicate):
                self.context.check()
                if (
                    triple in self.consumed
                    or not isinstance(triple.subject, RDFBlank)
                    or triple.object != RDFIRI(RDF + "nil")
                ):
                    continue
                marker = Triple(
                    triple.subject,
                    RDFIRI(RDF + "type"),
                    RDFIRI(OWL + "Class"),
                )
                if marker in self.consumed or not self.graph.contains(marker):
                    continue
                self.graph.one(triple.subject, predicate)
                has_other_constructor = any(
                    candidate != predicate and self.graph.objects(triple.subject, candidate)
                    for candidate in (
                        OWL + "intersectionOf",
                        OWL + "unionOf",
                        OWL + "complementOf",
                        OWL + "oneOf",
                    )
                ) or self.graph.contains(
                    Triple(
                        triple.subject,
                        RDFIRI(RDF + "type"),
                        RDFIRI(OWL + "Restriction"),
                    )
                )
                if has_other_constructor:
                    self._mapping_error("empty class boolean has conflicting constructors")
                self._class_expression(triple.subject)

    def _consume_detached_named_class_booleans(self) -> None:
        for predicate in (OWL + "intersectionOf", OWL + "unionOf"):
            for triple in self.graph.find(predicate=predicate):
                self.context.check()
                if triple in self.consumed or not isinstance(triple.subject, RDFBlank):
                    continue
                marker = Triple(
                    triple.subject,
                    RDFIRI(RDF + "type"),
                    RDFIRI(OWL + "Class"),
                )
                if marker in self.consumed or not self.graph.contains(marker):
                    continue
                if not self._is_established_named_list(
                    triple.object,
                    m.EntityKind.CLASS,
                    minimum_arity=1,
                ):
                    continue
                self.graph.one(triple.subject, predicate)
                has_other_constructor = any(
                    candidate != predicate and self.graph.objects(triple.subject, candidate)
                    for candidate in (
                        OWL + "intersectionOf",
                        OWL + "unionOf",
                        OWL + "complementOf",
                        OWL + "oneOf",
                    )
                ) or self.graph.contains(
                    Triple(
                        triple.subject,
                        RDFIRI(RDF + "type"),
                        RDFIRI(OWL + "Restriction"),
                    )
                )
                if has_other_constructor:
                    self._mapping_error("class boolean has conflicting constructors")
                self._class_expression(triple.subject)

    def _is_established_named_list(
        self,
        head: RDFTerm,
        expected_kind: m.EntityKind,
        *,
        minimum_arity: int,
    ) -> bool:
        if not isinstance(head, RDFBlank):
            return False
        visited: set[RDFBlank] = set()
        current = head
        length = 0
        while True:
            self.context.check()
            length += 1
            self.context.limits.enforce("max_rdf_list_length", length)
            self.context.limits.enforce("max_sequence_arity", length)
            if current in visited:
                return False
            visited.add(current)
            firsts = self.graph.objects(current, RDF + "first")
            rests = self.graph.objects(current, RDF + "rest")
            if len(firsts) != 1 or len(rests) != 1:
                return False
            first = firsts[0]
            if not isinstance(first, RDFIRI):
                return False
            if expected_kind not in self.kinds.get(first.value, set()) and not (
                (
                    expected_kind is m.EntityKind.CLASS
                    and first.value in _BUILTIN_CLASSES
                )
                or (
                    expected_kind is m.EntityKind.DATATYPE
                    and first.value in _BUILTIN_DATATYPES
                )
            ):
                return False
            rest = rests[0]
            if isinstance(rest, RDFIRI) and rest.value == RDF + "nil":
                return length >= minimum_arity
            if not isinstance(rest, RDFBlank):
                return False
            current = rest

    def _consume_detached_class_complements(self) -> None:
        for triple in self.graph.find(predicate=OWL + "complementOf"):
            self.context.check()
            if (
                triple in self.consumed
                or not isinstance(triple.subject, RDFBlank)
                or not isinstance(triple.object, RDFIRI)
                or (
                    m.EntityKind.CLASS not in self.kinds.get(triple.object.value, set())
                    and triple.object.value not in _BUILTIN_CLASSES
                )
            ):
                continue
            marker = Triple(
                triple.subject,
                RDFIRI(RDF + "type"),
                RDFIRI(OWL + "Class"),
            )
            if marker in self.consumed or not self.graph.contains(marker):
                continue
            self._class_expression(triple.subject)

    def _consume_detached_object_enumerations(self) -> None:
        for triple in self.graph.find(predicate=OWL + "oneOf"):
            self.context.check()
            if triple in self.consumed or not isinstance(triple.subject, RDFBlank):
                continue
            marker = Triple(
                triple.subject,
                RDFIRI(RDF + "type"),
                RDFIRI(OWL + "Class"),
            )
            if marker in self.consumed or not self.graph.contains(marker):
                continue
            self.graph.one(triple.subject, OWL + "oneOf")
            has_other_constructor = any(
                self.graph.objects(triple.subject, OWL + predicate)
                for predicate in (
                    "intersectionOf",
                    "unionOf",
                    "complementOf",
                )
            ) or self.graph.contains(
                Triple(
                    triple.subject,
                    RDFIRI(RDF + "type"),
                    RDFIRI(OWL + "Restriction"),
                )
            )
            if has_other_constructor:
                self._mapping_error("object enumeration has conflicting constructors")
            self._class_expression(triple.subject)

    def _consume_detached_named_data_booleans(self) -> None:
        for predicate in (OWL + "intersectionOf", OWL + "unionOf"):
            for triple in self.graph.find(predicate=predicate):
                self.context.check()
                if triple in self.consumed or not isinstance(triple.subject, RDFBlank):
                    continue
                marker = Triple(
                    triple.subject,
                    RDFIRI(RDF + "type"),
                    RDFIRI(RDFS + "Datatype"),
                )
                if marker in self.consumed or not self.graph.contains(marker):
                    continue
                if not self._is_established_named_list(
                    triple.object,
                    m.EntityKind.DATATYPE,
                    minimum_arity=2,
                ):
                    continue
                self.graph.one(triple.subject, predicate)
                has_other_constructor = any(
                    candidate != predicate and self.graph.objects(triple.subject, candidate)
                    for candidate in (
                        OWL + "intersectionOf",
                        OWL + "unionOf",
                        OWL + "oneOf",
                        OWL + "datatypeComplementOf",
                        OWL + "onDatatype",
                        OWL + "withRestrictions",
                    )
                )
                has_compatibility_marker = self.graph.contains(
                    Triple(
                        triple.subject,
                        RDFIRI(RDF + "type"),
                        RDFIRI(OWL + "DataRange"),
                    )
                )
                if has_other_constructor or has_compatibility_marker:
                    self._mapping_error("data boolean has conflicting constructors or markers")
                self._data_range(triple.subject)

    def _consume_detached_datatype_restrictions(self) -> None:
        for triple in self.graph.find(predicate=OWL + "onDatatype"):
            self.context.check()
            if (
                triple in self.consumed
                or not isinstance(triple.subject, RDFBlank)
                or not isinstance(triple.object, RDFIRI)
                or (
                    m.EntityKind.DATATYPE not in self.kinds.get(triple.object.value, set())
                    and triple.object.value not in _BUILTIN_DATATYPES
                )
            ):
                continue
            marker = Triple(
                triple.subject,
                RDFIRI(RDF + "type"),
                RDFIRI(RDFS + "Datatype"),
            )
            if marker in self.consumed or not self.graph.contains(marker):
                continue
            restrictions = self.graph.objects(triple.subject, OWL + "withRestrictions")
            if not any(self._is_established_facet_list(head) for head in restrictions):
                continue
            self.graph.one(triple.subject, OWL + "onDatatype")
            self.graph.one(triple.subject, OWL + "withRestrictions")
            has_other_constructor = any(
                self.graph.objects(triple.subject, OWL + predicate)
                for predicate in (
                    "intersectionOf",
                    "unionOf",
                    "oneOf",
                    "datatypeComplementOf",
                )
            )
            has_compatibility_marker = self.graph.contains(
                Triple(
                    triple.subject,
                    RDFIRI(RDF + "type"),
                    RDFIRI(OWL + "DataRange"),
                )
            )
            if has_other_constructor or has_compatibility_marker:
                self._mapping_error(
                    "datatype restriction has conflicting constructors or markers"
                )
            self._data_range(triple.subject)

    def _is_established_facet_list(self, head: RDFTerm) -> bool:
        if not isinstance(head, RDFBlank):
            return False
        visited: set[RDFBlank] = set()
        current = head
        length = 0
        while True:
            self.context.check()
            length += 1
            self.context.limits.enforce("max_rdf_list_length", length)
            self.context.limits.enforce("max_sequence_arity", length)
            if current in visited:
                return False
            visited.add(current)
            firsts = self.graph.objects(current, RDF + "first")
            rests = self.graph.objects(current, RDF + "rest")
            if len(firsts) != 1 or len(rests) != 1:
                return False
            facet = firsts[0]
            if not isinstance(facet, RDFBlank) or not any(
                isinstance(candidate.object, RDFLiteral)
                for candidate in self.graph.find(subject=facet)
            ):
                return False
            rest = rests[0]
            if isinstance(rest, RDFIRI) and rest.value == RDF + "nil":
                return True
            if not isinstance(rest, RDFBlank):
                return False
            current = rest

    def _consume_detached_data_complements(self) -> None:
        for triple in self.graph.find(predicate=OWL + "datatypeComplementOf"):
            self.context.check()
            if (
                triple in self.consumed
                or not isinstance(triple.subject, RDFBlank)
                or not isinstance(triple.object, RDFIRI)
                or (
                    m.EntityKind.DATATYPE not in self.kinds.get(triple.object.value, set())
                    and triple.object.value not in _BUILTIN_DATATYPES
                )
            ):
                continue
            marker = Triple(
                triple.subject,
                RDFIRI(RDF + "type"),
                RDFIRI(RDFS + "Datatype"),
            )
            if marker in self.consumed or not self.graph.contains(marker):
                continue
            self._data_range(triple.subject)

    def _consume_detached_data_enumerations(self) -> None:
        for triple in self.graph.find(predicate=OWL + "oneOf"):
            self.context.check()
            if triple in self.consumed or not isinstance(triple.subject, RDFBlank):
                continue
            marker = Triple(
                triple.subject,
                RDFIRI(RDF + "type"),
                RDFIRI(RDFS + "Datatype"),
            )
            if marker in self.consumed or not self.graph.contains(marker):
                continue
            self._data_range(triple.subject)

    def _consume_detached_owl1_data_enumerations(self) -> None:
        for marker in self.graph.find(
            predicate=RDF + "type",
            object=RDFIRI(OWL + "DataRange"),
        ):
            self.context.check()
            if marker in self.consumed or not isinstance(marker.subject, RDFBlank):
                continue
            one_of = self.graph.one(marker.subject, OWL + "oneOf")
            has_other_constructor = any(
                self.graph.objects(marker.subject, OWL + predicate)
                for predicate in (
                    "intersectionOf",
                    "unionOf",
                    "datatypeComplementOf",
                    "onDatatype",
                    "withRestrictions",
                )
            )
            if one_of is None and not has_other_constructor:
                continue
            if one_of is not None and has_other_constructor:
                self._mapping_error("OWL 1 data range has conflicting constructors")
            self._data_range(marker.subject)

    def _collect_axiom_annotations(self) -> None:
        annotation_nodes: dict[Triple, list[RDFResource]] = {}
        for type_triple in self.graph.find(
            predicate=RDF + "type", object=RDFIRI(OWL + "Annotation")
        ):
            node = type_triple.subject
            main = self._reification_main(node, "owl:Annotation")
            if not self.graph.contains(main):
                raise OntologySyntaxError(
                    "owl:Annotation reification main triple is absent",
                    code="RDF_AXIOM_REIFICATION",
                )
            annotation_nodes.setdefault(main, []).append(node)
            self.unclaimed_nested_reifications.add(main)

        visiting: set[Triple] = set()

        def nested(main: Triple) -> m.CanonicalSet[m.Annotation]:
            cached = self.annotation_annotations.get(main)
            if cached is not None:
                return cached
            if main in visiting:
                raise OntologySyntaxError(
                    "cyclic annotated annotation reification",
                    code="RDF_AXIOM_REIFICATION",
                )
            visiting.add(main)
            values: list[m.Annotation] = []
            for node in annotation_nodes.get(main, []):
                for item in self.graph.find(subject=node):
                    self._consume(item)
                    if item.predicate.value not in _REIFICATION_METADATA:
                        self.context.limits.enforce("max_annotations", len(values) + 1)
                        self.annotation_dependencies.setdefault(main, set()).add(item)
                        values.append(
                            m.Annotation(
                                m.AnnotationProperty(m.IRI(item.predicate.value)),
                                self._annotation_value(item.object),
                                nested(item),
                            )
                        )
            visiting.remove(main)
            result = m.CanonicalSet(values)
            self.annotation_annotations[main] = result
            return result

        for main in annotation_nodes:
            nested(main)

        collected_axiom_annotations: dict[Triple, list[m.Annotation]] = {}
        for type_triple in self.graph.find(predicate=RDF + "type", object=RDFIRI(OWL + "Axiom")):
            node = type_triple.subject
            if self.graph.contains(
                Triple(node, RDFIRI(RDF + "type"), RDFIRI(OWL + "Annotation"))
            ):
                raise OntologySyntaxError(
                    "RDF reification node cannot be both owl:Axiom and owl:Annotation",
                    code="RDF_AXIOM_REIFICATION",
                )
            main = self._reification_main(node, "owl:Axiom")
            if not self.graph.contains(main):
                raise OntologySyntaxError(
                    "owl:Axiom reification main triple is absent",
                    code="RDF_AXIOM_REIFICATION",
                )
            self.unclaimed_axiom_reifications.add(main)
            metadata = {
                RDF + "type",
                OWL + "annotatedSource",
                OWL + "annotatedProperty",
                OWL + "annotatedTarget",
            }
            annotations = collected_axiom_annotations.setdefault(main, [])
            for item in self.graph.find(subject=node):
                self._consume(item)
                if item.predicate.value not in metadata:
                    self.context.limits.enforce("max_annotations", len(annotations) + 1)
                    self.annotation_dependencies.setdefault(main, set()).add(item)
                    annotations.append(
                        m.Annotation(
                            m.AnnotationProperty(m.IRI(item.predicate.value)),
                            self._annotation_value(item.object),
                            nested(item),
                        )
                    )
        self.axiom_annotations.update(
            (main, m.CanonicalSet(annotations))
            for main, annotations in collected_axiom_annotations.items()
        )

    def _reification_main(self, node: RDFResource, label: str) -> Triple:
        try:
            source = self.graph.one(node, OWL + "annotatedSource", required=True)
            predicate = self.graph.one(node, OWL + "annotatedProperty", required=True)
            target = self.graph.one(node, OWL + "annotatedTarget", required=True)
        except OntologySyntaxError as error:
            raise OntologySyntaxError(
                f"{label} reification metadata is incomplete or ambiguous",
                code="RDF_AXIOM_REIFICATION",
            ) from error
        if not isinstance(source, (RDFIRI, RDFBlank)) or not isinstance(predicate, RDFIRI):
            raise OntologySyntaxError(
                f"{label} reification has invalid source/property",
                code="RDF_AXIOM_REIFICATION",
            )
        return Triple(source, predicate, cast(RDFTerm, target))

    def _header(
        self,
    ) -> tuple[OntologyID, tuple[m.IRI, ...], tuple[m.Annotation, ...], RDFResource | None]:
        headers = self.graph.find(predicate=RDF + "type", object=RDFIRI(OWL + "Ontology"))
        if len(headers) > 1:
            raise OntologySyntaxError(
                "RDF graph contains more than one ontology header",
                code="RDF_ONTOLOGY_HEADER",
            )
        if not headers:
            return OntologyID(), (), (), None
        header = headers[0]
        self._consume(header)
        node = header.subject
        ontology_iri = m.IRI(node.value) if isinstance(node, RDFIRI) else None
        version = self.graph.one(node, OWL + "versionIRI")
        version_iri: m.IRI | None = None
        if version is not None:
            if not isinstance(version, RDFIRI):
                raise OntologySyntaxError(
                    "owl:versionIRI must be an IRI", code="RDF_ONTOLOGY_HEADER"
                )
            version_iri = m.IRI(version.value)
            self._consume_only(node, OWL + "versionIRI", version)
        imports: list[m.IRI] = []
        for value in self.graph.objects(node, OWL + "imports"):
            if not isinstance(value, RDFIRI):
                raise OntologySyntaxError(
                    "owl:imports target must be an IRI", code="RDF_IMPORT_IRI"
                )
            imports.append(m.IRI(value.value))
            self._consume_only(node, OWL + "imports", value)
        structural = {RDF + "type", OWL + "versionIRI", OWL + "imports"}
        annotations: list[m.Annotation] = []
        for triple in self.graph.find(subject=node):
            if triple.predicate.value in structural or triple in self.consumed:
                continue
            if self._is_annotation_property(triple.predicate.value):
                annotations.append(self._annotation_from_triple(triple))
                self._consume(triple)
        return OntologyID(ontology_iri, version_iri), tuple(imports), tuple(annotations), node

    def _declarations(self) -> tuple[m.AxiomNode, ...]:
        mapping = {
            OWL + "Class": m.Class,
            RDFS + "Datatype": m.Datatype,
            OWL + "ObjectProperty": m.ObjectProperty,
            OWL + "DatatypeProperty": m.DataProperty,
            OWL + "AnnotationProperty": m.AnnotationProperty,
            OWL + "OntologyProperty": m.AnnotationProperty,
            OWL + "NamedIndividual": m.NamedIndividual,
        }
        inferred = {
            OWL + "InverseFunctionalProperty": m.ObjectProperty,
            OWL + "SymmetricProperty": m.ObjectProperty,
            OWL + "TransitiveProperty": m.ObjectProperty,
        }
        values: list[m.AxiomNode] = []
        for triple in self.graph.find(predicate=RDF + "type"):
            if not isinstance(triple.subject, RDFIRI) or not isinstance(triple.object, RDFIRI):
                continue
            constructor = mapping.get(triple.object.value)
            is_inferred = constructor is None
            if is_inferred:
                constructor = inferred.get(triple.object.value)
            if constructor is None:
                continue
            if is_inferred and self.graph.contains(
                Triple(
                    triple.subject,
                    RDFIRI(RDF + "type"),
                    RDFIRI(OWL + "ObjectProperty"),
                )
            ):
                continue
            annotations = (
                m.CanonicalSet()
                if is_inferred
                else self.axiom_annotations.get(triple, m.CanonicalSet())
            )
            values.append(m.Declaration(constructor(m.IRI(triple.subject.value)), annotations))
            if not is_inferred:
                self._consume(triple)
                self._claim_axiom_reifications(triple)
        return tuple(values)

    def _special_axioms(self) -> tuple[m.AxiomNode, ...]:
        values: list[m.AxiomNode] = []
        for type_triple in self.graph.find(predicate=RDF + "type"):
            if not isinstance(type_triple.object, RDFIRI):
                continue
            kind = type_triple.object.value
            node = type_triple.subject
            if kind == OWL + "NegativePropertyAssertion":
                source = self.graph.one(node, OWL + "sourceIndividual", required=True)
                prop = self.graph.one(node, OWL + "assertionProperty", required=True)
                target_i = self.graph.one(node, OWL + "targetIndividual")
                target_v = self.graph.one(node, OWL + "targetValue")
                if not isinstance(source, (RDFIRI, RDFBlank)) or not isinstance(
                    prop, (RDFIRI, RDFBlank)
                ):
                    self._mapping_error("invalid negative property assertion")
                annotations = self._annotations_on_node(node, _NEGATIVE_METADATA)
                if (target_i is None) == (target_v is None):
                    self._mapping_error("negative assertion requires exactly one target")
                if target_i is not None:
                    if not isinstance(target_i, (RDFIRI, RDFBlank)):
                        self._mapping_error("negative object assertion target must be a resource")
                    special_value: m.AxiomNode = m.NegativeObjectPropertyAssertion(
                        self._object_property(prop),
                        self._individual(source),
                        self._individual(target_i),
                        annotations,
                    )
                else:
                    if not isinstance(target_v, RDFLiteral):
                        self._mapping_error("negative data assertion target must be a literal")
                    if not isinstance(prop, RDFIRI):
                        self._mapping_error("negative data assertion property must be named")
                    special_value = m.NegativeDataPropertyAssertion(
                        m.DataProperty(m.IRI(prop.value)),
                        self._individual(source),
                        self._literal(target_v),
                        annotations,
                    )
                self._consume_subject(node)
                values.append(special_value)
            elif kind in {
                OWL + "AllDisjointClasses",
                OWL + "AllDisjointProperties",
                OWL + "AllDifferent",
            }:
                predicate = OWL + ("distinctMembers" if kind == OWL + "AllDifferent" else "members")
                head = self.graph.one(node, predicate, required=True)
                members = self._list(cast(RDFTerm, head))
                annotations = self._annotations_on_node(node, {RDF + "type", predicate})
                if kind == OWL + "AllDisjointClasses":
                    expressions = tuple(self._class_expression(item) for item in members)
                    if len(expressions) < 2:
                        self._mapping_error(
                            "all-disjoint class axiom requires at least two members"
                        )
                    collection_value: m.AxiomNode = _disjoint_classes(
                        expressions, annotations
                    )
                elif kind == OWL + "AllDifferent":
                    individuals = m.CanonicalSet(
                        self._individual_resource(item) for item in members
                    )
                    if len(individuals) < 2:
                        self._mapping_error(
                            "different-individuals axiom requires at least two "
                            "distinct members"
                        )
                    collection_value = m.DifferentIndividuals(
                        individuals,
                        annotations,
                    )
                else:
                    if all(self._resource_iri(item) in self.data_kinds for item in members):
                        data_properties = m.CanonicalSet(
                            self._data_property_term(item) for item in members
                        )
                        if len(data_properties) < 2:
                            self._mapping_error(
                                "all-disjoint property axiom requires at least two "
                                "distinct members"
                            )
                        collection_value = m.DisjointDataProperties(
                            data_properties, annotations
                        )
                    else:
                        object_properties = m.CanonicalSet(
                            self._object_property(item) for item in members
                        )
                        if len(object_properties) < 2:
                            self._mapping_error(
                                "all-disjoint property axiom requires at least two "
                                "distinct members"
                            )
                        collection_value = m.DisjointObjectProperties(
                            object_properties, annotations
                        )
                self._consume_subject(node)
                values.append(collection_value)
        return tuple(values)

    def _swrl_rules(self) -> tuple[SWRLRule, ...]:
        values: list[SWRLRule] = []
        for type_triple in self.graph.find(
            predicate=RDF + "type", object=RDFIRI(SWRL + "Imp")
        ):
            if not self.allow_swrl:
                raise UnsupportedSyntaxError(
                    "SWRL extension requires explicit enablement",
                    code="RDF_EXTENSION_DISABLED",
                )
            node = type_triple.subject
            body_head = cast(RDFTerm, self.graph.one(node, SWRL + "body", required=True))
            head_head = cast(RDFTerm, self.graph.one(node, SWRL + "head", required=True))
            body: m.CanonicalSet[Atom] = m.CanonicalSet(
                self._swrl_atom(item) for item in self._list(body_head)
            )
            head: m.CanonicalSet[Atom] = m.CanonicalSet(
                self._swrl_atom(item) for item in self._list(head_head)
            )
            self.context.limits.enforce("max_rule_atoms", max(len(body), len(head)))
            annotations = self._annotations_on_node(node, _SWRL_RULE_METADATA)
            values.append(SWRLRule(body, head, annotations))
            self._consume_subject(node)
        return tuple(values)

    def _swrl_atom(self, term: RDFTerm) -> Atom:
        if not isinstance(term, (RDFIRI, RDFBlank)):
            self._mapping_type("SWRL atom must be an RDF resource")
        atom_type = self.graph.one(term, RDF + "type", required=True)
        if not isinstance(atom_type, RDFIRI):
            self._mapping_type("SWRL atom type must be an IRI")
        kind = atom_type.value
        if kind == SWRL + "ClassAtom":
            self._ensure_predicates(term, _SWRL_CLASS_ATOM_METADATA)
            predicate = cast(
                RDFTerm, self.graph.one(term, SWRL + "classPredicate", required=True)
            )
            if not isinstance(predicate, (RDFIRI, RDFBlank)):
                self._mapping_type("SWRL class predicate must be a resource")
            argument = cast(RDFTerm, self.graph.one(term, SWRL + "argument1", required=True))
            value: Atom = ClassAtom(
                self._class_expression(predicate), self._swrl_individual_argument(argument)
            )
        elif kind == SWRL + "DataRangeAtom":
            self._ensure_predicates(term, _SWRL_DATA_RANGE_ATOM_METADATA)
            predicate = cast(RDFTerm, self.graph.one(term, SWRL + "dataRange", required=True))
            if not isinstance(predicate, (RDFIRI, RDFBlank)):
                self._mapping_type("SWRL data-range predicate must be a resource")
            argument = cast(RDFTerm, self.graph.one(term, SWRL + "argument1", required=True))
            value = DataRangeAtom(
                self._data_range(predicate), self._swrl_data_argument(argument)
            )
        elif kind == SWRL + "IndividualPropertyAtom":
            self._ensure_predicates(term, _SWRL_PROPERTY_ATOM_METADATA)
            predicate = cast(
                RDFTerm, self.graph.one(term, SWRL + "propertyPredicate", required=True)
            )
            if not isinstance(predicate, (RDFIRI, RDFBlank)):
                self._mapping_type("SWRL object-property predicate must be a resource")
            first = cast(RDFTerm, self.graph.one(term, SWRL + "argument1", required=True))
            second = cast(RDFTerm, self.graph.one(term, SWRL + "argument2", required=True))
            value = ObjectPropertyAtom(
                self._object_property(predicate),
                self._swrl_individual_argument(first),
                self._swrl_individual_argument(second),
            )
        elif kind == SWRL + "DatavaluedPropertyAtom":
            self._ensure_predicates(term, _SWRL_PROPERTY_ATOM_METADATA)
            predicate = cast(
                RDFTerm, self.graph.one(term, SWRL + "propertyPredicate", required=True)
            )
            if not isinstance(predicate, RDFIRI):
                self._mapping_type("SWRL data-property predicate must be a named IRI")
            first = cast(RDFTerm, self.graph.one(term, SWRL + "argument1", required=True))
            second = cast(RDFTerm, self.graph.one(term, SWRL + "argument2", required=True))
            value = DataPropertyAtom(
                m.DataProperty(m.IRI(predicate.value)),
                self._swrl_individual_argument(first),
                self._swrl_data_argument(second),
            )
        elif kind == SWRL + "BuiltinAtom":
            self._ensure_predicates(term, _SWRL_BUILTIN_ATOM_METADATA)
            predicate = cast(RDFTerm, self.graph.one(term, SWRL + "builtin", required=True))
            if not isinstance(predicate, RDFIRI):
                self._mapping_type("SWRL builtin predicate must be a named IRI")
            arguments_head = cast(
                RDFTerm, self.graph.one(term, SWRL + "arguments", required=True)
            )
            value = BuiltInAtom(
                m.IRI(predicate.value),
                tuple(self._swrl_data_argument(item) for item in self._list(arguments_head)),
            )
        elif kind in {SWRL + "SameIndividualAtom", SWRL + "DifferentIndividualsAtom"}:
            self._ensure_predicates(term, _SWRL_INDIVIDUAL_ATOM_METADATA)
            first = cast(RDFTerm, self.graph.one(term, SWRL + "argument1", required=True))
            second = cast(RDFTerm, self.graph.one(term, SWRL + "argument2", required=True))
            constructor = (
                SameIndividualAtom
                if kind == SWRL + "SameIndividualAtom"
                else DifferentIndividualsAtom
            )
            value = constructor(
                self._swrl_individual_argument(first),
                self._swrl_individual_argument(second),
            )
        else:
            self._mapping_type("unknown SWRL atom type")
        self._consume_subject(term)
        return value

    def _swrl_individual_argument(self, term: RDFTerm) -> IndividualArgument:
        variable = self._swrl_variable(term)
        if variable is not None:
            return variable
        if not isinstance(term, (RDFIRI, RDFBlank)):
            self._mapping_type("SWRL individual argument must be a resource")
        return self._individual(term)

    def _swrl_data_argument(self, term: RDFTerm) -> DataArgument:
        variable = self._swrl_variable(term)
        if variable is not None:
            return variable
        if not isinstance(term, RDFLiteral):
            self._mapping_type("SWRL data argument must be a variable or literal")
        return self._literal(term)

    def _swrl_variable(self, term: RDFTerm) -> Variable | None:
        if not isinstance(term, (RDFIRI, RDFBlank)):
            return None
        markers = self.graph.find(
            subject=term, predicate=RDF + "type", object=RDFIRI(SWRL + "Variable")
        )
        if not markers:
            return None
        if not isinstance(term, RDFIRI):
            self._mapping_type("SWRL variables must be named IRIs")
        for marker in markers:
            self._consume(marker)
        return Variable(m.IRI(term.value))

    def _ensure_predicates(self, subject: RDFResource, allowed: set[str]) -> None:
        if any(
            triple.predicate.value not in allowed for triple in self.graph.find(subject=subject)
        ):
            raise UnsupportedSyntaxError(
                "SWRL atom has an unsupported predicate",
                code="RDF_MAPPING_INCOMPLETE",
            )

    def _equivalence_components(self) -> tuple[m.AxiomNode, ...]:
        values: list[m.AxiomNode] = []
        values.extend(self._component_axioms(OWL + "equivalentClass", "class"))
        values.extend(self._component_axioms(OWL + "equivalentProperty", "property"))
        values.extend(self._component_axioms(OWL + "sameAs", "same"))
        return tuple(values)

    def _component_axioms(self, predicate: str, kind: str) -> tuple[m.AxiomNode, ...]:
        triples = [
            item for item in self.graph.find(predicate=predicate) if item not in self.consumed
        ]
        adjacency: dict[RDFTerm, set[RDFTerm]] = {}
        valid: list[Triple] = []
        for triple in triples:
            if isinstance(triple.object, RDFLiteral):
                continue
            if kind == "class" and isinstance(triple.subject, RDFIRI):
                subject_kinds = self.kinds.get(triple.subject.value, set())
                if m.EntityKind.DATATYPE in subject_kinds:
                    continue
            adjacency.setdefault(triple.subject, set()).add(triple.object)
            adjacency.setdefault(triple.object, set()).add(triple.subject)
            valid.append(triple)
        output: list[m.AxiomNode] = []
        seen: set[RDFTerm] = set()
        for start in sorted(adjacency, key=_term_key):
            if start in seen:
                continue
            queue = [start]
            component: set[RDFTerm] = set()
            while queue:
                current = queue.pop()
                if current in component:
                    continue
                component.add(current)
                queue.extend(adjacency[current] - component)
            seen.update(component)
            edges = [
                item for item in valid if item.subject in component and item.object in component
            ]
            annotations = m.CanonicalSet(
                annotation
                for edge in edges
                for annotation in self.axiom_annotations.get(edge, m.CanonicalSet())
            )
            for edge in edges:
                self._consume(edge)
                self._claim_axiom_reifications(edge)
            ordered = tuple(sorted(component, key=_term_key))
            if kind == "class":
                expressions = m.CanonicalSet(map(self._class_expression, ordered))
                if len(expressions) < 2:
                    self._mapping_error(
                        "equivalent-classes axiom requires at least two distinct members"
                    )
                output.append(
                    m.EquivalentClasses(expressions, annotations)
                )
            elif kind == "same":
                individuals = m.CanonicalSet(map(self._individual_resource, ordered))
                if len(individuals) < 2:
                    self._mapping_error(
                        "same-individual axiom requires at least two distinct members"
                    )
                output.append(
                    m.SameIndividual(individuals, annotations)
                )
            elif all(self._resource_iri(item) in self.data_kinds for item in ordered):
                data_properties = m.CanonicalSet(
                    map(self._data_property_term, ordered)
                )
                if len(data_properties) < 2:
                    self._mapping_error(
                        "equivalent-data-properties axiom requires at least two "
                        "distinct members"
                    )
                output.append(
                    m.EquivalentDataProperties(data_properties, annotations)
                )
            else:
                object_properties = m.CanonicalSet(
                    map(self._object_property, ordered)
                )
                if len(object_properties) < 2:
                    self._mapping_error(
                        "equivalent-object-properties axiom requires at least two "
                        "distinct members"
                    )
                output.append(
                    m.EquivalentObjectProperties(object_properties, annotations)
                )
        return tuple(output)

    def _simple_axiom(
        self, triple: Triple, ontology_node: RDFResource | None
    ) -> m.AxiomNode | None:
        p = triple.predicate.value
        s = triple.subject
        o = triple.object
        annotations = self.axiom_annotations.get(triple, m.CanonicalSet())
        value: m.AxiomNode | None = None
        if p == RDFS + "subClassOf" and not isinstance(o, RDFLiteral):
            value = m.SubClassOf(self._class_expression(s), self._class_expression(o), annotations)
        elif p == OWL + "disjointWith" and not isinstance(o, RDFLiteral):
            value = _disjoint_classes(
                (self._class_expression(s), self._class_expression(o)),
                annotations,
            )
        elif p == OWL + "disjointUnionOf":
            expressions = m.CanonicalSet(
                self._class_expression(item) for item in self._list(o)
            )
            if len(expressions) < 2:
                self._mapping_error(
                    "disjoint-union axiom requires at least two distinct members"
                )
            value = m.DisjointUnion(
                self._class_resource(s),
                expressions,
                annotations,
            )
        elif (
            isinstance(s, RDFIRI)
            and (
                m.EntityKind.CLASS in self.kinds.get(s.value, set())
                or s.value in _BUILTIN_CLASSES
            )
            and p
            in {
                OWL + "complementOf",
                OWL + "intersectionOf",
                OWL + "oneOf",
                OWL + "unionOf",
            }
        ):
            expression = (
                self._owl1_named_class_complement(o)
                if p == OWL + "complementOf"
                else self._class_list_expression(
                    p,
                    o,
                    compatibility=True,
                    named_individuals_only=True,
                )
            )
            equivalent_expressions = m.CanonicalSet(
                (m.Class(m.IRI(s.value)), expression)
            )
            if len(equivalent_expressions) < 2:
                self._mapping_error(
                    "named class constructor axiom requires two distinct expressions"
                )
            value = m.EquivalentClasses(
                equivalent_expressions, annotations
            )
        elif p == RDFS + "subPropertyOf" and isinstance(s, RDFIRI) and isinstance(o, RDFIRI):
            if s.value in self.annotation_kinds or o.value in self.annotation_kinds:
                value = m.SubAnnotationPropertyOf(
                    m.AnnotationProperty(m.IRI(s.value)),
                    m.AnnotationProperty(m.IRI(o.value)),
                    annotations,
                )
            elif s.value in self.data_kinds or o.value in self.data_kinds:
                value = m.SubDataPropertyOf(
                    m.DataProperty(m.IRI(s.value)), m.DataProperty(m.IRI(o.value)), annotations
                )
            else:
                value = m.SubObjectPropertyOf(
                    m.ObjectProperty(m.IRI(s.value)), m.ObjectProperty(m.IRI(o.value)), annotations
                )
        elif p == OWL + "propertyChainAxiom" and isinstance(s, RDFIRI):
            properties = tuple(self._object_property(item) for item in self._list(o))
            if len(properties) < 2:
                self._mapping_cardinality(
                    "object property chain requires at least two members"
                )
            chain = m.ObjectPropertyChain(properties)
            value = m.SubObjectPropertyOf(chain, m.ObjectProperty(m.IRI(s.value)), annotations)
        elif p == OWL + "propertyDisjointWith" and isinstance(o, RDFIRI):
            if isinstance(s, RDFIRI) and s.value in self.data_kinds:
                disjoint_data_properties = m.CanonicalSet(
                    (m.DataProperty(m.IRI(s.value)), m.DataProperty(m.IRI(o.value)))
                )
                if len(disjoint_data_properties) < 2:
                    self._mapping_error(
                        "disjoint-data-properties axiom requires at least two "
                        "distinct members"
                    )
                value = m.DisjointDataProperties(
                    disjoint_data_properties, annotations
                )
            else:
                disjoint_object_properties = m.CanonicalSet(
                    (self._object_property(s), self._object_property(o))
                )
                if len(disjoint_object_properties) < 2:
                    self._mapping_error(
                        "disjoint-object-properties axiom requires at least two "
                        "distinct members"
                    )
                value = m.DisjointObjectProperties(
                    disjoint_object_properties, annotations
                )
        elif (
            p == OWL + "inverseOf"
            and isinstance(o, (RDFIRI, RDFBlank))
            and not self._looks_expression(s)
        ):
            value = m.InverseObjectProperties(
                self._object_property(s), self._object_property(o), annotations
            )
        elif p in {RDFS + "domain", RDFS + "range"} and not isinstance(o, RDFLiteral):
            if isinstance(s, RDFIRI) and s.value in self.annotation_kinds:
                value = (
                    m.AnnotationPropertyDomain(
                        m.AnnotationProperty(m.IRI(s.value)), self._iri_resource(o), annotations
                    )
                    if p.endswith("domain")
                    else m.AnnotationPropertyRange(
                        m.AnnotationProperty(m.IRI(s.value)), self._iri_resource(o), annotations
                    )
                )
            elif isinstance(s, RDFIRI) and s.value in self.data_kinds:
                value = (
                    m.DataPropertyDomain(
                        m.DataProperty(m.IRI(s.value)), self._class_expression(o), annotations
                    )
                    if p.endswith("domain")
                    else m.DataPropertyRange(
                        m.DataProperty(m.IRI(s.value)), self._data_range(o), annotations
                    )
                )
            else:
                value = (
                    m.ObjectPropertyDomain(
                        self._object_property(s), self._class_expression(o), annotations
                    )
                    if p.endswith("domain")
                    else m.ObjectPropertyRange(
                        self._object_property(s), self._class_expression(o), annotations
                    )
                )
        elif p == OWL + "hasKey":
            members = self._list(o)
            object_properties = [
                self._object_property(item)
                for item in members
                if self._resource_iri(item) not in self.data_kinds
            ]
            data_properties = [
                self._data_property_term(item)
                for item in members
                if self._resource_iri(item) in self.data_kinds
            ]
            if not object_properties and not data_properties:
                self._mapping_cardinality("has-key axiom requires at least one property")
            value = m.HasKey(
                self._class_expression(s),
                m.CanonicalSet(object_properties),
                m.CanonicalSet(data_properties),
                annotations,
            )
        elif p == OWL + "differentFrom" and not isinstance(o, RDFLiteral):
            individuals = m.CanonicalSet(
                (self._individual_resource(s), self._individual_resource(o))
            )
            if len(individuals) < 2:
                self._mapping_error(
                    "different-individuals axiom requires at least two distinct members"
                )
            value = m.DifferentIndividuals(
                individuals, annotations
            )
        elif (
            p == RDF + "type"
            and isinstance(s, RDFIRI)
            and isinstance(o, RDFIRI)
            and o.value in {OWL + "DeprecatedClass", OWL + "DeprecatedProperty"}
        ):
            value = m.AnnotationAssertion(
                m.AnnotationProperty(m.IRI(OWL + "deprecated")),
                m.IRI(s.value),
                m.Literal("true", m.Datatype(m.IRI(XSD + "boolean"))),
                annotations,
            )
        elif p == RDF + "type" and isinstance(o, (RDFIRI, RDFBlank)):
            characteristic = _CHARACTERISTIC_TYPES.get(o.value) if isinstance(o, RDFIRI) else None
            if characteristic is not None:
                if characteristic == "FunctionalDataProperty" or (
                    characteristic == "FunctionalProperty"
                    and isinstance(s, RDFIRI)
                    and s.value in self.data_kinds
                ):
                    value = m.FunctionalDataProperty(self._data_property_term(s), annotations)
                else:
                    constructor_name = (
                        "FunctionalObjectProperty"
                        if characteristic == "FunctionalProperty"
                        else characteristic
                    )
                    value = cast(
                        m.AxiomNode,
                        getattr(m, constructor_name)(self._object_property(s), annotations),
                    )
            elif isinstance(o, RDFBlank) or not _is_structural_type(o.value):
                value = m.ClassAssertion(
                    self._class_expression(o), self._individual_resource(s), annotations
                )
        elif (
            p == OWL + "equivalentClass"
            and isinstance(s, RDFIRI)
            and m.EntityKind.DATATYPE in self.kinds.get(s.value, set())
        ):
            value = m.DatatypeDefinition(
                m.Datatype(m.IRI(s.value)), self._data_range(cast(RDFResource, o)), annotations
            )
        elif self._is_annotation_property(p):
            if ontology_node is not None and s == ontology_node:
                return None
            value = m.AnnotationAssertion(
                m.AnnotationProperty(m.IRI(p)),
                self._annotation_subject(s),
                self._annotation_value(o),
                annotations,
            )
        elif isinstance(o, RDFLiteral) and p not in _STRUCTURAL_PREDICATES:
            if p in self.data_kinds:
                value = m.DataPropertyAssertion(
                    m.DataProperty(m.IRI(p)),
                    self._individual_resource(s),
                    self._literal(o),
                    annotations,
                )
        elif (
            isinstance(o, (RDFIRI, RDFBlank))
            and p not in _STRUCTURAL_PREDICATES
            and p in self.object_kinds
        ):
            value = m.ObjectPropertyAssertion(
                m.ObjectProperty(m.IRI(p)),
                self._individual_resource(s),
                self._individual_resource(o),
                annotations,
            )
        if value is not None:
            self._consume(triple)
            self._claim_axiom_reifications(triple)
        return value

    def _class_expression(self, term: RDFTerm) -> m.ClassExpression:
        if isinstance(term, RDFIRI):
            return m.Class(m.IRI(term.value))
        if not isinstance(term, RDFBlank):
            self._mapping_error("class expression cannot be a literal")
        self._claim(term, "expression")
        key = ("class", term)
        self._enter(key)
        try:
            for predicate in (
                OWL + "intersectionOf",
                OWL + "unionOf",
                OWL + "oneOf",
            ):
                head = self.graph.one(term, predicate)
                if head is not None:
                    self._consume_only(term, predicate, head)
                    compatibility = self._consume_marker(term, OWL + "Class")
                    return self._class_list_expression(
                        predicate,
                        head,
                        compatibility=compatibility,
                        named_individuals_only=False,
                    )
            complement = self.graph.one(term, OWL + "complementOf")
            if complement is not None:
                if isinstance(complement, RDFLiteral):
                    self._mapping_error("owl:complementOf target cannot be a literal")
                self._consume_only(term, OWL + "complementOf", complement)
                self._consume_marker(term, OWL + "Class")
                return m.ObjectComplementOf(self._class_expression(complement))
            if self.graph.find(
                subject=term, predicate=RDF + "type", object=RDFIRI(OWL + "Restriction")
            ):
                return self._restriction(term)
            self._mapping_error("blank node is not a recognized class expression")
        finally:
            self._leave(key)

    def _class_list_expression(
        self,
        predicate: str,
        head: RDFTerm,
        *,
        compatibility: bool,
        named_individuals_only: bool,
    ) -> m.ClassExpression:
        items = self._list(head)
        if not items:
            if not compatibility:
                self._mapping_error("boolean or enumeration class expression has no operands")
            return m.OWL_THING if predicate == OWL + "intersectionOf" else m.OWL_NOTHING
        if predicate == OWL + "oneOf":
            if named_individuals_only and not all(isinstance(item, RDFIRI) for item in items):
                self._mapping_error("OWL 1 named enumeration requires named individuals")
            return m.ObjectOneOf(m.CanonicalSet(map(self._individual_resource, items)))
        if len(items) == 1:
            if not compatibility:
                self._mapping_error("boolean class expression has fewer than two operands")
            return self._class_expression(items[0])
        expressions = m.CanonicalSet(map(self._class_expression, items))
        if len(expressions) == 1:
            return next(iter(expressions))
        return (
            m.ObjectIntersectionOf(expressions)
            if predicate == OWL + "intersectionOf"
            else m.ObjectUnionOf(expressions)
        )

    def _owl1_named_class_complement(self, target: RDFTerm) -> m.ClassExpression:
        if isinstance(target, RDFLiteral):
            self._mapping_error("owl:complementOf target cannot be a literal")
        return m.ObjectComplementOf(self._class_expression(target))

    def _restriction(self, term: RDFBlank) -> m.ClassExpression:
        self._consume_marker(term, OWL + "Restriction")
        on_properties = self.graph.one(term, OWL + "onProperties")
        on_property = self.graph.one(term, OWL + "onProperty")
        if (on_properties is None) == (on_property is None):
            self._mapping_error("restriction requires exactly one property selector")
        if on_properties is not None:
            properties = tuple(self._data_property_term(item) for item in self._list(on_properties))
            if not properties:
                self._mapping_error("n-ary data restriction requires at least one property")
            self._consume_only(term, OWL + "onProperties", on_properties)
            some = self.graph.one(term, OWL + "someValuesFrom")
            all_value = self.graph.one(term, OWL + "allValuesFrom")
            if (some is None) == (all_value is None):
                self._mapping_error("n-ary data restriction requires one quantifier")
            quantified_filler = some if some is not None else all_value
            if quantified_filler is None or isinstance(quantified_filler, RDFLiteral):
                self._mapping_error("data restriction filler cannot be literal")
            predicate = OWL + ("someValuesFrom" if some is not None else "allValuesFrom")
            self._consume_only(term, predicate, quantified_filler)
            return (
                m.DataSomeValuesFrom(properties, self._data_range(quantified_filler))
                if some is not None
                else m.DataAllValuesFrom(properties, self._data_range(quantified_filler))
            )
        if not isinstance(on_property, (RDFIRI, RDFBlank)):
            self._mapping_error("owl:onProperty target must be a resource")
        self._consume_only(term, OWL + "onProperty", on_property)
        property_iri = self._resource_iri(on_property)
        is_data = property_iri in self.data_kinds
        for predicate, object_constructor, data_constructor in (
            (OWL + "someValuesFrom", m.ObjectSomeValuesFrom, m.DataSomeValuesFrom),
            (OWL + "allValuesFrom", m.ObjectAllValuesFrom, m.DataAllValuesFrom),
        ):
            filler = self.graph.one(term, predicate)
            if filler is not None:
                if isinstance(filler, RDFLiteral):
                    self._mapping_error("quantified restriction filler cannot be literal")
                self._consume_only(term, predicate, filler)
                if is_data or self._looks_data_range(filler):
                    return cast(
                        m.ClassExpression,
                        data_constructor(
                            (self._data_property_term(on_property),),
                            self._data_range(filler),
                        ),
                    )
                return cast(
                    m.ClassExpression,
                    object_constructor(
                        self._object_property(on_property), self._class_expression(filler)
                    ),
                )
        has_value = self.graph.one(term, OWL + "hasValue")
        if has_value is not None:
            self._consume_only(term, OWL + "hasValue", has_value)
            if isinstance(has_value, RDFLiteral):
                return m.DataHasValue(
                    self._data_property_term(on_property), self._literal(has_value)
                )
            return m.ObjectHasValue(
                self._object_property(on_property), self._individual_resource(has_value)
            )
        has_self = self.graph.one(term, OWL + "hasSelf")
        if has_self is not None:
            if not isinstance(has_self, RDFLiteral) or has_self.lexical.lower() != "true":
                self._mapping_error("owl:hasSelf must be true")
            self._consume_only(term, OWL + "hasSelf", has_self)
            return m.ObjectHasSelf(self._object_property(on_property))
        cardinalities = (
            (OWL + "minCardinality", "Min", False),
            (OWL + "maxCardinality", "Max", False),
            (OWL + "cardinality", "Exact", False),
            (OWL + "minQualifiedCardinality", "Min", True),
            (OWL + "maxQualifiedCardinality", "Max", True),
            (OWL + "qualifiedCardinality", "Exact", True),
        )
        for predicate, suffix, qualified in cardinalities:
            cardinality = self.graph.one(term, predicate)
            if cardinality is None:
                continue
            number = self._nonnegative(cardinality)
            self._consume_only(term, predicate, cardinality)
            on_class = self.graph.one(term, OWL + "onClass")
            on_data = self.graph.one(term, OWL + "onDataRange")
            if qualified and (on_class is None) == (on_data is None):
                self._mapping_error("qualified cardinality requires one qualified filler")
            if on_data is not None or (not qualified and is_data):
                cardinality_data_filler: m.DataRange = m.RDFS_LITERAL
                if on_data is not None:
                    if isinstance(on_data, RDFLiteral):
                        self._mapping_error("owl:onDataRange cannot be a literal")
                    self._consume_only(term, OWL + "onDataRange", on_data)
                    cardinality_data_filler = self._data_range(on_data)
                return cast(
                    m.ClassExpression,
                    getattr(m, "Data" + suffix + "Cardinality")(
                        number,
                        self._data_property_term(on_property),
                        cardinality_data_filler,
                    ),
                )
            filler_class: m.ClassExpression = m.OWL_THING
            if on_class is not None:
                if isinstance(on_class, RDFLiteral):
                    self._mapping_error("owl:onClass cannot be a literal")
                self._consume_only(term, OWL + "onClass", on_class)
                filler_class = self._class_expression(on_class)
            return cast(
                m.ClassExpression,
                getattr(m, "Object" + suffix + "Cardinality")(
                    number, self._object_property(on_property), filler_class
                ),
            )
        self._mapping_error("unrecognized owl:Restriction")

    def _data_range(self, term: RDFTerm) -> m.DataRange:
        if isinstance(term, RDFIRI):
            return m.Datatype(m.IRI(term.value))
        if not isinstance(term, RDFBlank):
            self._mapping_error("data range cannot be a literal")
        self._claim(term, "expression")
        key = ("data", term)
        self._enter(key)
        try:
            for predicate in (
                OWL + "intersectionOf",
                OWL + "unionOf",
                OWL + "oneOf",
            ):
                head = self.graph.one(term, predicate)
                if head is not None:
                    self._consume_only(term, predicate, head)
                    standard_marker = self._consume_marker(term, RDFS + "Datatype")
                    compatibility_marker = self._consume_marker(term, OWL + "DataRange")
                    if standard_marker and compatibility_marker:
                        self._mapping_error("data range has conflicting datatype markers")
                    if compatibility_marker and predicate != OWL + "oneOf":
                        self._mapping_error("OWL 1 data range marker requires owl:oneOf")
                    items = self._list(head)
                    if predicate == OWL + "oneOf":
                        if not items:
                            if compatibility_marker:
                                return m.DataComplementOf(m.RDFS_LITERAL)
                            self._mapping_error("data enumeration has no literal values")
                        if not all(isinstance(item, RDFLiteral) for item in items):
                            self._mapping_error("data enumeration must contain literals")
                        return m.DataOneOf(
                            m.CanonicalSet(self._literal(cast(RDFLiteral, item)) for item in items)
                        )
                    if len(items) < 2:
                        self._mapping_error("boolean data range has fewer than two operands")
                    ranges = m.CanonicalSet(map(self._data_range, items))
                    if len(ranges) == 1:
                        return next(iter(ranges))
                    return (
                        m.DataIntersectionOf(ranges)
                        if predicate == OWL + "intersectionOf"
                        else m.DataUnionOf(ranges)
                    )
            complement = self.graph.one(term, OWL + "datatypeComplementOf")
            if complement is not None:
                if isinstance(complement, RDFLiteral):
                    self._mapping_error("datatype complement target cannot be literal")
                self._consume_only(term, OWL + "datatypeComplementOf", complement)
                standard_marker = self._consume_marker(term, RDFS + "Datatype")
                compatibility_marker = self._consume_marker(term, OWL + "DataRange")
                if standard_marker and compatibility_marker:
                    self._mapping_error("data range has conflicting datatype markers")
                if compatibility_marker:
                    self._mapping_error("OWL 1 data range marker requires owl:oneOf")
                return m.DataComplementOf(self._data_range(complement))
            datatype = self.graph.one(term, OWL + "onDatatype")
            restrictions = self.graph.one(term, OWL + "withRestrictions")
            if datatype is not None or restrictions is not None:
                if not isinstance(datatype, RDFIRI) or restrictions is None:
                    self._mapping_error("datatype restriction is incomplete")
                self._consume_only(term, OWL + "onDatatype", datatype)
                self._consume_only(term, OWL + "withRestrictions", restrictions)
                standard_marker = self._consume_marker(term, RDFS + "Datatype")
                compatibility_marker = self._consume_marker(term, OWL + "DataRange")
                if standard_marker and compatibility_marker:
                    self._mapping_error("data range has conflicting datatype markers")
                if compatibility_marker:
                    self._mapping_error("OWL 1 data range marker requires owl:oneOf")
                facets: list[m.FacetRestriction] = []
                facet_items = self._list(restrictions)
                if not facet_items:
                    self._mapping_error("datatype restriction requires at least one facet")
                for item in facet_items:
                    if not isinstance(item, RDFBlank):
                        self._mapping_error("facet restriction list item must be blank")
                    self._claim(item, "facet")
                    candidates = [
                        triple
                        for triple in self.graph.find(subject=item)
                        if isinstance(triple.object, RDFLiteral)
                    ]
                    if len(candidates) != 1:
                        self._mapping_error("facet restriction must have one literal facet triple")
                    facet = candidates[0]
                    self._consume(facet)
                    facets.append(
                        m.FacetRestriction(
                            m.IRI(facet.predicate.value),
                            self._literal(cast(RDFLiteral, facet.object)),
                        )
                    )
                return m.DatatypeRestriction(
                    m.Datatype(m.IRI(datatype.value)), m.CanonicalSet(facets)
                )
            self._mapping_error("blank node is not a recognized data range")
        finally:
            self._leave(key)

    def _object_property(self, term: RDFTerm) -> m.ObjectPropertyExpression:
        if isinstance(term, RDFIRI):
            return m.ObjectProperty(m.IRI(term.value))
        if not isinstance(term, RDFBlank):
            self._mapping_error("object property expression cannot be literal")
        self._claim(term, "expression")
        inverse = self.graph.one(term, OWL + "inverseOf", required=True)
        if not isinstance(inverse, RDFIRI):
            self._mapping_error("inverse property target must be named")
        self._consume_only(term, OWL + "inverseOf", inverse)
        return m.ObjectInverseOf(m.ObjectProperty(m.IRI(inverse.value)))

    def _list(self, head: RDFTerm) -> tuple[RDFTerm, ...]:
        if isinstance(head, RDFIRI) and head.value == RDF + "nil":
            return ()
        if not isinstance(head, RDFBlank):
            self._mapping_error("RDF collection head must be blank or rdf:nil")
        root = head
        items: list[RDFTerm] = []
        visited: set[RDFBlank] = set()
        current = head
        while True:
            self.context.limits.enforce("max_rdf_list_length", len(items) + 1)
            self.context.limits.enforce("max_sequence_arity", len(items) + 1)
            if current in visited:
                self._mapping_error("cyclic RDF collection")
            visited.add(current)
            owner = self.list_nodes.setdefault(current, root)
            if owner != root:
                self._mapping_error("shared RDF collection tail")
            self._claim(current, "list")
            self._consume_marker(current, RDF + "List")
            first = self.graph.one(current, RDF + "first", required=True)
            rest = self.graph.one(current, RDF + "rest", required=True)
            first_term = cast(RDFTerm, first)
            rest_term = cast(RDFTerm, rest)
            self._consume_only(current, RDF + "first", first_term)
            self._consume_only(current, RDF + "rest", rest_term)
            items.append(first_term)
            if isinstance(rest_term, RDFIRI) and rest_term.value == RDF + "nil":
                return tuple(items)
            if not isinstance(rest_term, RDFBlank):
                self._mapping_error("RDF collection tail must be blank or rdf:nil")
            current = rest_term

    def _annotations_on_node(
        self, node: RDFResource, metadata: set[str]
    ) -> m.CanonicalSet[m.Annotation]:
        values: list[m.Annotation] = []
        for triple in self.graph.find(subject=node):
            if triple.predicate.value not in metadata:
                self.context.limits.enforce("max_annotations", len(values) + 1)
                values.append(self._annotation_from_triple(triple))
        return m.CanonicalSet(values)

    def _annotation(self, predicate: RDFIRI, value: RDFTerm) -> m.Annotation:
        return m.Annotation(
            m.AnnotationProperty(m.IRI(predicate.value)), self._annotation_value(value)
        )

    def _annotation_from_triple(self, triple: Triple) -> m.Annotation:
        self._claim_nested_reifications(triple)
        return m.Annotation(
            m.AnnotationProperty(m.IRI(triple.predicate.value)),
            self._annotation_value(triple.object),
            self.annotation_annotations.get(triple, m.CanonicalSet()),
        )

    def _claim_axiom_reifications(self, main: Triple) -> None:
        self.unclaimed_axiom_reifications.discard(main)
        for annotation in self.annotation_dependencies.get(main, set()):
            self._claim_nested_reifications(annotation)

    def _claim_nested_reifications(self, main: Triple) -> None:
        if main not in self.unclaimed_nested_reifications:
            return
        self.unclaimed_nested_reifications.remove(main)
        for annotation in self.annotation_dependencies.get(main, set()):
            self._claim_nested_reifications(annotation)

    def _annotation_value(self, value: RDFTerm) -> m.AnnotationValue:
        if isinstance(value, RDFIRI):
            return m.IRI(value.value)
        if isinstance(value, RDFBlank):
            return cast(m.AnonymousIndividual, self._individual(value))
        return self._literal(value)

    def _annotation_subject(self, value: RDFResource) -> m.AnnotationSubject:
        return (
            m.IRI(value.value)
            if isinstance(value, RDFIRI)
            else cast(m.AnonymousIndividual, self._individual(value))
        )

    def _individual_resource(self, value: RDFTerm) -> m.Individual:
        if not isinstance(value, (RDFIRI, RDFBlank)):
            self._mapping_error("individual must be an RDF resource")
        return self._individual(value)

    def _individual(self, value: RDFResource) -> m.Individual:
        if isinstance(value, RDFIRI):
            return m.NamedIndividual(m.IRI(value.value))
        self._claim(value, "individual")
        return provisional_anonymous(value.label)

    def _literal(self, value: RDFLiteral) -> m.Literal:
        try:
            if value.language is not None:
                return m.Literal(value.lexical, m.RDF_PLAIN_LITERAL, value.language)
            if value.datatype == m.RDF_PLAIN_LITERAL_IRI and value.lexical.endswith("@"):
                return m.Literal(value.lexical[:-1], m.RDF_PLAIN_LITERAL)
            datatype = m.XSD_STRING if value.datatype is None else m.Datatype(m.IRI(value.datatype))
            return m.Literal(value.lexical, datatype)
        except InvalidLiteralError as error:
            self._mapping_error(str(error))

    def _class_resource(self, value: RDFResource) -> m.Class:
        if not isinstance(value, RDFIRI):
            self._mapping_error("defined class must be named")
        return m.Class(m.IRI(value.value))

    def _data_property_term(self, value: RDFTerm) -> m.DataProperty:
        if not isinstance(value, RDFIRI):
            self._mapping_error("data property must be named")
        return m.DataProperty(m.IRI(value.value))

    @staticmethod
    def _iri_resource(value: RDFResource) -> m.IRI:
        if not isinstance(value, RDFIRI):
            raise OntologySyntaxError("IRI value cannot be blank", code="RDF_MAPPING_TYPE")
        return m.IRI(value.value)

    @staticmethod
    def _resource_iri(value: RDFTerm) -> str:
        return value.value if isinstance(value, RDFIRI) else ""

    def _nonnegative(self, value: RDFTerm) -> int:
        if not isinstance(value, RDFLiteral) or not value.lexical.isdigit():
            self._mapping_error("cardinality must be a nonnegative integer literal")
        return int(value.lexical)

    def _looks_data_range(self, value: RDFResource) -> bool:
        if isinstance(value, RDFIRI):
            return value.value in {
                iri for iri, kinds in self.kinds.items() if m.EntityKind.DATATYPE in kinds
            }
        return bool(
            self.graph.objects(value, OWL + "onDatatype")
            or self.graph.find(
                subject=value, predicate=RDF + "type", object=RDFIRI(RDFS + "Datatype")
            )
        )

    def _looks_expression(self, value: RDFResource) -> bool:
        return isinstance(value, RDFBlank) and bool(self.graph.objects(value, OWL + "inverseOf"))

    def _is_annotation_property(self, value: str) -> bool:
        return value in self.annotation_kinds or value in _BUILTIN_ANNOTATION_PROPERTIES

    def _consume(self, triple: Triple) -> None:
        self.consumed.add(triple)

    def _consume_only(self, subject: RDFResource, predicate: str, object: RDFTerm) -> None:
        triple = Triple(subject, RDFIRI(predicate), object)
        if not self.graph.contains(triple):
            self._mapping_error("mapping attempted to consume an absent triple")
        self._consume(triple)

    def _consume_marker(self, subject: RDFResource, object_iri: str) -> bool:
        triple = Triple(subject, RDFIRI(RDF + "type"), RDFIRI(object_iri))
        if self.graph.contains(triple):
            self._consume(triple)
            return True
        return False

    def _consume_subject(self, subject: RDFResource) -> None:
        for triple in self.graph.find(subject=subject):
            self._consume(triple)

    def _claim(self, node: RDFBlank, role: str) -> None:
        current = self.blank_roles.get(node.label)
        if current is not None and current != role and {current, role} != {"expression", "list"}:
            self._mapping_error(f"blank node is ambiguously used as {current} and {role}")
        self.blank_roles[node.label] = role

    def _enter(self, key: tuple[str, RDFTerm]) -> None:
        if key in self.stack:
            self._mapping_error("cyclic RDF structural expression")
        self.stack.add(key)
        self.context.depth(len(self.stack))

    def _leave(self, key: tuple[str, RDFTerm]) -> None:
        self.stack.remove(key)

    @staticmethod
    def _mapping_error(message: str) -> NoReturn:
        raise UnsupportedSyntaxError(message, code="RDF_MAPPING_UNSUPPORTED")

    @staticmethod
    def _mapping_cardinality(message: str) -> NoReturn:
        raise OntologySyntaxError(message, code="RDF_MAPPING_CARDINALITY")

    @staticmethod
    def _mapping_type(message: str) -> NoReturn:
        raise UnsupportedSyntaxError(message, code="RDF_MAPPING_TYPE")


class RDFEncoder:
    __slots__ = ("blank_counter", "encoded_nodes", "expression_nodes", "triples")

    def __init__(self) -> None:
        self.triples: set[Triple] = set()
        self.expression_nodes: dict[bytes, RDFBlank] = {}
        self.encoded_nodes: set[RDFBlank] = set()
        self.blank_counter = 0

    def encode(self, document: OntologyDocument) -> RDFGraph:
        ontology: RDFResource = (
            RDFIRI(document.ontology_id.ontology_iri.value)
            if document.ontology_id.ontology_iri is not None
            else RDFBlank("ontology")
        )
        self.add(ontology, RDF + "type", RDFIRI(OWL + "Ontology"))
        if document.ontology_id.version_iri is not None:
            self.add(ontology, OWL + "versionIRI", RDFIRI(document.ontology_id.version_iri.value))
        for item in document.direct_imports:
            self.add(ontology, OWL + "imports", RDFIRI(item.value))
        for annotation in document.ontology_annotations:
            self._annotation_triple(ontology, annotation)
        for axiom in document.axioms:
            self._axiom(axiom)
        if document.extension_components:
            raise UnsupportedSyntaxError(
                "RDF SWRL rendering is not enabled in the core OWL-only writer",
                code="RDF_EXTENSION_UNSUPPORTED",
            )
        return RDFGraph(self.triples)

    def add(self, subject: RDFResource, predicate: str, object: RDFTerm) -> Triple:
        triple = Triple(subject, RDFIRI(predicate), object)
        self.triples.add(triple)
        return triple

    def _axiom(self, value: m.AxiomNode) -> None:
        main: Triple | None = None
        special: RDFResource | None = None
        if isinstance(value, m.Declaration):
            types = {
                m.EntityKind.CLASS: OWL + "Class",
                m.EntityKind.DATATYPE: RDFS + "Datatype",
                m.EntityKind.OBJECT_PROPERTY: OWL + "ObjectProperty",
                m.EntityKind.DATA_PROPERTY: OWL + "DatatypeProperty",
                m.EntityKind.ANNOTATION_PROPERTY: OWL + "AnnotationProperty",
                m.EntityKind.NAMED_INDIVIDUAL: OWL + "NamedIndividual",
            }
            main = self.add(
                RDFIRI(value.entity.iri.value), RDF + "type", RDFIRI(types[value.entity.kind])
            )
        elif isinstance(value, m.SubClassOf):
            main = self.add(
                self._class(value.sub_class), RDFS + "subClassOf", self._class(value.super_class)
            )
        elif isinstance(value, m.EquivalentClasses):
            members = [self._class(item) for item in value.expressions]
            for left, right in pairwise(members):
                current = self.add(left, OWL + "equivalentClass", right)
                main = current if main is None else main
        elif isinstance(value, m.DisjointClasses):
            members = [self._class(item) for item in value.expressions]
            if len(members) == 2:
                main = self.add(members[0], OWL + "disjointWith", members[1])
            else:
                special = self._fresh("disjoint-classes")
                self.add(special, RDF + "type", RDFIRI(OWL + "AllDisjointClasses"))
                self.add(special, OWL + "members", self._list(members))
        elif isinstance(value, m.DisjointUnion):
            main = self.add(
                RDFIRI(value.defined_class.iri.value),
                OWL + "disjointUnionOf",
                self._list([self._class(item) for item in value.expressions]),
            )
        elif isinstance(value, m.SubObjectPropertyOf):
            if isinstance(value.sub_property, m.ObjectPropertyChain):
                main = self.add(
                    self._object_property(value.super_property),
                    OWL + "propertyChainAxiom",
                    self._list(
                        [self._object_property(item) for item in value.sub_property.properties]
                    ),
                )
            else:
                main = self.add(
                    self._object_property(value.sub_property),
                    RDFS + "subPropertyOf",
                    self._object_property(value.super_property),
                )
        elif isinstance(value, (m.EquivalentObjectProperties, m.EquivalentDataProperties)):
            members = [self._property(item) for item in value.properties]
            for left, right in pairwise(members):
                current = self.add(left, OWL + "equivalentProperty", right)
                main = current if main is None else main
        elif isinstance(value, (m.DisjointObjectProperties, m.DisjointDataProperties)):
            members = [self._property(item) for item in value.properties]
            if len(members) == 2:
                main = self.add(members[0], OWL + "propertyDisjointWith", members[1])
            else:
                special = self._fresh("disjoint-properties")
                self.add(special, RDF + "type", RDFIRI(OWL + "AllDisjointProperties"))
                self.add(special, OWL + "members", self._list(members))
        elif isinstance(value, m.InverseObjectProperties):
            main = self.add(
                self._object_property(value.first),
                OWL + "inverseOf",
                self._object_property(value.second),
            )
        elif isinstance(value, (m.ObjectPropertyDomain, m.DataPropertyDomain)):
            main = self.add(
                self._property(value.property), RDFS + "domain", self._class(value.domain)
            )
        elif isinstance(value, m.AnnotationPropertyDomain):
            main = self.add(
                RDFIRI(value.property.iri.value), RDFS + "domain", RDFIRI(value.domain.value)
            )
        elif isinstance(value, m.ObjectPropertyRange):
            main = self.add(
                self._object_property(value.property), RDFS + "range", self._class(value.range)
            )
        elif isinstance(value, m.DataPropertyRange):
            main = self.add(
                RDFIRI(value.property.iri.value), RDFS + "range", self._data_range(value.range)
            )
        elif isinstance(value, m.AnnotationPropertyRange):
            main = self.add(
                RDFIRI(value.property.iri.value), RDFS + "range", RDFIRI(value.range.value)
            )
        elif type(value).__name__ in _CHARACTERISTIC_AXIOMS:
            characteristic_value = cast(Any, value)
            main = self.add(
                self._property(characteristic_value.property),
                RDF + "type",
                RDFIRI(_CHARACTERISTIC_AXIOMS[type(value).__name__]),
            )
        elif isinstance(value, m.SubDataPropertyOf):
            main = self.add(
                RDFIRI(value.sub_property.iri.value),
                RDFS + "subPropertyOf",
                RDFIRI(value.super_property.iri.value),
            )
        elif isinstance(value, m.DatatypeDefinition):
            main = self.add(
                RDFIRI(value.datatype.iri.value),
                OWL + "equivalentClass",
                self._data_range(value.data_range),
            )
        elif isinstance(value, m.HasKey):
            members = [self._object_property(item) for item in value.object_properties]
            members.extend(RDFIRI(item.iri.value) for item in value.data_properties)
            main = self.add(
                self._class(value.class_expression), OWL + "hasKey", self._list(members)
            )
        elif isinstance(value, m.SameIndividual):
            members = [self._individual(item) for item in value.individuals]
            for left, right in pairwise(members):
                current = self.add(left, OWL + "sameAs", right)
                main = current if main is None else main
        elif isinstance(value, m.DifferentIndividuals):
            members = [self._individual(item) for item in value.individuals]
            if len(members) == 2:
                main = self.add(members[0], OWL + "differentFrom", members[1])
            else:
                special = self._fresh("different")
                self.add(special, RDF + "type", RDFIRI(OWL + "AllDifferent"))
                self.add(special, OWL + "distinctMembers", self._list(members))
        elif isinstance(value, m.ClassAssertion):
            main = self.add(
                self._individual(value.individual),
                RDF + "type",
                self._class(value.class_expression),
            )
        elif isinstance(value, m.ObjectPropertyAssertion):
            if isinstance(value.property, m.ObjectInverseOf):
                assertion_subject = self._individual(value.target)
                assertion_predicate = value.property.property.iri.value
                assertion_object = self._individual(value.source)
            else:
                assertion_subject = self._individual(value.source)
                assertion_predicate = value.property.iri.value
                assertion_object = self._individual(value.target)
            main = self.add(
                assertion_subject,
                assertion_predicate,
                assertion_object,
            )
        elif isinstance(value, m.DataPropertyAssertion):
            main = self.add(
                self._individual(value.source), value.property.iri.value, self._literal(value.value)
            )
        elif isinstance(
            value, (m.NegativeObjectPropertyAssertion, m.NegativeDataPropertyAssertion)
        ):
            special = self._fresh("negative")
            self.add(special, RDF + "type", RDFIRI(OWL + "NegativePropertyAssertion"))
            self.add(special, OWL + "sourceIndividual", self._individual(value.source))
            self.add(special, OWL + "assertionProperty", self._property(value.property))
            if isinstance(value, m.NegativeObjectPropertyAssertion):
                self.add(special, OWL + "targetIndividual", self._individual(value.target))
            else:
                self.add(special, OWL + "targetValue", self._literal(value.value))
        elif isinstance(value, m.AnnotationAssertion):
            main = self.add(
                self._annotation_subject(value.subject),
                value.property.iri.value,
                self._annotation_value(value.value),
            )
        elif isinstance(value, m.SubAnnotationPropertyOf):
            main = self.add(
                RDFIRI(value.sub_property.iri.value),
                RDFS + "subPropertyOf",
                RDFIRI(value.super_property.iri.value),
            )
        else:
            raise UnsupportedSyntaxError(
                f"RDF writer has no branch for {type(value).__name__}",
                code="RDF_WRITER_DISPATCH",
            )
        axiom_annotations = cast(m.CanonicalSet[m.Annotation], cast(Any, value).annotations)
        if axiom_annotations:
            if special is not None:
                for annotation in axiom_annotations:
                    self._annotation_triple(special, annotation)
            elif main is not None:
                self._reify(main, axiom_annotations)

    def _class(self, value: m.ClassExpression) -> RDFResource:
        if isinstance(value, m.Class):
            return RDFIRI(value.iri.value)
        node = self._expression_node(value)
        if node in self.encoded_nodes:
            return node
        self.encoded_nodes.add(node)
        self.add(
            node, RDF + "type", RDFIRI(OWL + ("Restriction" if _is_restriction(value) else "Class"))
        )
        if isinstance(value, m.ObjectIntersectionOf):
            self.add(
                node,
                OWL + "intersectionOf",
                self._list([self._class(item) for item in value.operands]),
            )
        elif isinstance(value, m.ObjectUnionOf):
            self.add(
                node, OWL + "unionOf", self._list([self._class(item) for item in value.operands])
            )
        elif isinstance(value, m.ObjectComplementOf):
            self.add(node, OWL + "complementOf", self._class(value.operand))
        elif isinstance(value, m.ObjectOneOf):
            self.add(
                node,
                OWL + "oneOf",
                self._list([self._individual(item) for item in value.individuals]),
            )
        elif isinstance(value, (m.ObjectSomeValuesFrom, m.ObjectAllValuesFrom)):
            self.add(node, OWL + "onProperty", self._object_property(value.property))
            predicate = OWL + (
                "someValuesFrom" if isinstance(value, m.ObjectSomeValuesFrom) else "allValuesFrom"
            )
            self.add(node, predicate, self._class(value.filler))
        elif isinstance(value, m.ObjectHasValue):
            self.add(node, OWL + "onProperty", self._object_property(value.property))
            self.add(node, OWL + "hasValue", self._individual(value.value))
        elif isinstance(value, m.ObjectHasSelf):
            self.add(node, OWL + "onProperty", self._object_property(value.property))
            self.add(node, OWL + "hasSelf", RDFLiteral("true", XSD + "boolean"))
        elif isinstance(value, _OBJECT_CARDINALITY_TYPES):
            self.add(node, OWL + "onProperty", self._object_property(value.property))
            self.add(
                node,
                _cardinality_predicate(value, qualified=True),
                _cardinality_literal(value.cardinality),
            )
            self.add(node, OWL + "onClass", self._class(value.filler))
        elif isinstance(value, (m.DataSomeValuesFrom, m.DataAllValuesFrom)):
            if len(value.properties) == 1:
                self.add(node, OWL + "onProperty", RDFIRI(value.properties[0].iri.value))
            else:
                self.add(
                    node,
                    OWL + "onProperties",
                    self._list([RDFIRI(item.iri.value) for item in value.properties]),
                )
            predicate = OWL + (
                "someValuesFrom" if isinstance(value, m.DataSomeValuesFrom) else "allValuesFrom"
            )
            self.add(node, predicate, self._data_range(value.filler))
        elif isinstance(value, m.DataHasValue):
            self.add(node, OWL + "onProperty", RDFIRI(value.property.iri.value))
            self.add(node, OWL + "hasValue", self._literal(value.value))
        elif isinstance(value, _DATA_CARDINALITY_TYPES):
            self.add(node, OWL + "onProperty", RDFIRI(value.property.iri.value))
            self.add(
                node,
                _cardinality_predicate(value, qualified=True),
                _cardinality_literal(value.cardinality),
            )
            self.add(node, OWL + "onDataRange", self._data_range(value.filler))
        return node

    def _data_range(self, value: m.DataRange) -> RDFResource:
        if isinstance(value, m.Datatype):
            return RDFIRI(value.iri.value)
        node = self._expression_node(value)
        if node in self.encoded_nodes:
            return node
        self.encoded_nodes.add(node)
        self.add(node, RDF + "type", RDFIRI(RDFS + "Datatype"))
        if isinstance(value, m.DataIntersectionOf):
            self.add(
                node,
                OWL + "intersectionOf",
                self._list([self._data_range(item) for item in value.operands]),
            )
        elif isinstance(value, m.DataUnionOf):
            self.add(
                node,
                OWL + "unionOf",
                self._list([self._data_range(item) for item in value.operands]),
            )
        elif isinstance(value, m.DataComplementOf):
            self.add(node, OWL + "datatypeComplementOf", self._data_range(value.operand))
        elif isinstance(value, m.DataOneOf):
            self.add(
                node, OWL + "oneOf", self._list([self._literal(item) for item in value.values])
            )
        elif isinstance(value, m.DatatypeRestriction):
            self.add(node, OWL + "onDatatype", RDFIRI(value.datatype.iri.value))
            facets: list[RDFBlank] = []
            for restriction in value.restrictions:
                facet = self._fresh("facet")
                self.add(facet, restriction.facet.value, self._literal(restriction.value))
                facets.append(facet)
            self.add(node, OWL + "withRestrictions", self._list(facets))
        return node

    def _object_property(self, value: m.ObjectPropertyExpression) -> RDFResource:
        if isinstance(value, m.ObjectProperty):
            return RDFIRI(value.iri.value)
        node = self._expression_node(value)
        if node in self.encoded_nodes:
            return node
        self.encoded_nodes.add(node)
        self.add(node, OWL + "inverseOf", RDFIRI(value.property.iri.value))
        return node

    def _property(self, value: object) -> RDFResource:
        if isinstance(value, m.ObjectInverseOf):
            return self._object_property(value)
        if isinstance(value, m.Entity):
            return RDFIRI(value.iri.value)
        raise TypeError("expected property")

    @staticmethod
    def _individual(value: m.Individual) -> RDFResource:
        if isinstance(value, m.NamedIndividual):
            return RDFIRI(value.iri.value)
        return RDFBlank("i" + value.local_key.hex())

    @staticmethod
    def _literal(value: m.Literal) -> RDFLiteral:
        if value.language is not None:
            return RDFLiteral(value.lexical_form, language=value.language)
        if value.datatype == m.RDF_PLAIN_LITERAL:
            return RDFLiteral(value.lexical_form + "@", m.RDF_PLAIN_LITERAL_IRI)
        return RDFLiteral(value.lexical_form, value.datatype.iri.value)

    def _annotation_triple(self, subject: RDFResource, annotation: m.Annotation) -> Triple:
        triple = self.add(
            subject, annotation.property.iri.value, self._annotation_value(annotation.value)
        )
        if annotation.annotations:
            self._reify_annotation(triple, annotation.annotations)
        return triple

    def _reify(self, main: Triple, annotations: m.CanonicalSet[m.Annotation]) -> None:
        node = self._fresh("axiom")
        self.add(node, RDF + "type", RDFIRI(OWL + "Axiom"))
        self.add(node, OWL + "annotatedSource", main.subject)
        self.add(node, OWL + "annotatedProperty", main.predicate)
        self.add(node, OWL + "annotatedTarget", main.object)
        for annotation in annotations:
            self._annotation_triple(node, annotation)

    def _reify_annotation(self, main: Triple, annotations: m.CanonicalSet[m.Annotation]) -> None:
        node = self._fresh("annotation")
        self.add(node, RDF + "type", RDFIRI(OWL + "Annotation"))
        self.add(node, OWL + "annotatedSource", main.subject)
        self.add(node, OWL + "annotatedProperty", main.predicate)
        self.add(node, OWL + "annotatedTarget", main.object)
        for annotation in annotations:
            self._annotation_triple(node, annotation)

    @staticmethod
    def _annotation_subject(value: m.AnnotationSubject) -> RDFResource:
        return (
            RDFIRI(value.value)
            if isinstance(value, m.IRI)
            else RDFBlank("i" + value.local_key.hex())
        )

    def _annotation_value(self, value: m.AnnotationValue) -> RDFTerm:
        if isinstance(value, m.IRI):
            return RDFIRI(value.value)
        if isinstance(value, m.Literal):
            return self._literal(value)
        return RDFBlank("i" + value.local_key.hex())

    def _list(self, values: Sequence[RDFTerm]) -> RDFResource:
        if not values:
            return RDFIRI(RDF + "nil")
        nodes = [self._fresh("list") for _ in values]
        for index, (node, value) in enumerate(zip(nodes, values, strict=True)):
            self.add(node, RDF + "first", value)
            self.add(
                node,
                RDF + "rest",
                nodes[index + 1] if index + 1 < len(nodes) else RDFIRI(RDF + "nil"),
            )
        return nodes[0]

    def _expression_node(self, value: m.StructuralNode) -> RDFBlank:
        key = m.canonical_bytes(value)
        node = self.expression_nodes.get(key)
        if node is None:
            node = self._fresh("expr-" + hashlib.sha256(key).hexdigest()[:12])
            self.expression_nodes[key] = node
        return node

    def _fresh(self, stem: str) -> RDFBlank:
        self.blank_counter += 1
        return RDFBlank(f"{stem}-{self.blank_counter}")


def _disjoint_classes(
    expressions: Sequence[m.ClassExpression],
    annotations: m.CanonicalSet[m.Annotation],
) -> m.AxiomNode:
    canonical = m.CanonicalSet(expressions)
    if len(expressions) >= 2 and len(canonical) == 1:
        return m.SubClassOf(next(iter(canonical)), m.OWL_NOTHING, annotations)
    return m.DisjointClasses(canonical, annotations)


def _is_restriction(value: object) -> bool:
    return isinstance(
        value,
        (
            m.ObjectSomeValuesFrom,
            m.ObjectAllValuesFrom,
            m.ObjectHasValue,
            m.ObjectHasSelf,
            *_OBJECT_CARDINALITY_TYPES,
            m.DataSomeValuesFrom,
            m.DataAllValuesFrom,
            m.DataHasValue,
            *_DATA_CARDINALITY_TYPES,
        ),
    )


_OBJECT_CARDINALITY_TYPES = (
    m.ObjectMinCardinality,
    m.ObjectMaxCardinality,
    m.ObjectExactCardinality,
)
_DATA_CARDINALITY_TYPES = (
    m.DataMinCardinality,
    m.DataMaxCardinality,
    m.DataExactCardinality,
)


def _cardinality_predicate(value: object, *, qualified: bool) -> str:
    middle = (
        "min"
        if isinstance(value, (m.ObjectMinCardinality, m.DataMinCardinality))
        else ("max" if isinstance(value, (m.ObjectMaxCardinality, m.DataMaxCardinality)) else "")
    )
    if qualified:
        return OWL + (
            middle + "QualifiedCardinality" if middle else "qualifiedCardinality"
        )
    return OWL + middle + "Cardinality"


def _cardinality_literal(value: int) -> RDFLiteral:
    return RDFLiteral(str(value), XSD + "nonNegativeInteger")


_CHARACTERISTIC_AXIOMS = {
    "FunctionalObjectProperty": OWL + "FunctionalProperty",
    "FunctionalDataProperty": OWL + "FunctionalProperty",
    "InverseFunctionalObjectProperty": OWL + "InverseFunctionalProperty",
    "ReflexiveObjectProperty": OWL + "ReflexiveProperty",
    "IrreflexiveObjectProperty": OWL + "IrreflexiveProperty",
    "SymmetricObjectProperty": OWL + "SymmetricProperty",
    "AsymmetricObjectProperty": OWL + "AsymmetricProperty",
    "TransitiveObjectProperty": OWL + "TransitiveProperty",
}
_CHARACTERISTIC_TYPES = {
    OWL + "FunctionalProperty": "FunctionalProperty",
    OWL + "InverseFunctionalProperty": "InverseFunctionalObjectProperty",
    OWL + "ReflexiveProperty": "ReflexiveObjectProperty",
    OWL + "IrreflexiveProperty": "IrreflexiveObjectProperty",
    OWL + "SymmetricProperty": "SymmetricObjectProperty",
    OWL + "AsymmetricProperty": "AsymmetricObjectProperty",
    OWL + "TransitiveProperty": "TransitiveObjectProperty",
}
_NEGATIVE_METADATA = {
    RDF + "type",
    OWL + "sourceIndividual",
    OWL + "assertionProperty",
    OWL + "targetIndividual",
    OWL + "targetValue",
}
_REIFICATION_METADATA = {
    RDF + "type",
    OWL + "annotatedSource",
    OWL + "annotatedProperty",
    OWL + "annotatedTarget",
}
_SWRL_RULE_METADATA = {RDF + "type", SWRL + "body", SWRL + "head"}
_SWRL_CLASS_ATOM_METADATA = {
    RDF + "type",
    SWRL + "classPredicate",
    SWRL + "argument1",
}
_SWRL_DATA_RANGE_ATOM_METADATA = {
    RDF + "type",
    SWRL + "dataRange",
    SWRL + "argument1",
}
_SWRL_PROPERTY_ATOM_METADATA = {
    RDF + "type",
    SWRL + "propertyPredicate",
    SWRL + "argument1",
    SWRL + "argument2",
}
_SWRL_BUILTIN_ATOM_METADATA = {RDF + "type", SWRL + "builtin", SWRL + "arguments"}
_SWRL_INDIVIDUAL_ATOM_METADATA = {
    RDF + "type",
    SWRL + "argument1",
    SWRL + "argument2",
}
_BUILTIN_CLASSES = frozenset({OWL + "Thing", OWL + "Nothing"})
_BUILTIN_OBJECT_PROPERTIES = frozenset(
    {OWL + "topObjectProperty", OWL + "bottomObjectProperty"}
)
_BUILTIN_DATA_PROPERTIES = frozenset(
    {OWL + "topDataProperty", OWL + "bottomDataProperty"}
)
_BUILTIN_DATATYPES = frozenset(
    {
        RDFS + "Literal",
        RDF + "PlainLiteral",
        RDF + "XMLLiteral",
        OWL + "real",
        OWL + "rational",
        *(
            XSD + local
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
    }
)
_BUILTIN_ANNOTATION_PROPERTIES = {
    RDFS + "label",
    RDFS + "comment",
    RDFS + "seeAlso",
    RDFS + "isDefinedBy",
    OWL + "deprecated",
    OWL + "versionInfo",
    OWL + "priorVersion",
    OWL + "backwardCompatibleWith",
    OWL + "incompatibleWith",
}
_STRUCTURAL_PREDICATES = {
    RDF + "type",
    RDF + "first",
    RDF + "rest",
    RDFS + "subClassOf",
    RDFS + "subPropertyOf",
    RDFS + "domain",
    RDFS + "range",
    *{
        OWL + name
        for name in (
            "imports",
            "versionIRI",
            "intersectionOf",
            "unionOf",
            "complementOf",
            "oneOf",
            "datatypeComplementOf",
            "onDatatype",
            "withRestrictions",
            "onProperty",
            "onProperties",
            "someValuesFrom",
            "allValuesFrom",
            "hasValue",
            "hasSelf",
            "minCardinality",
            "maxCardinality",
            "cardinality",
            "minQualifiedCardinality",
            "maxQualifiedCardinality",
            "qualifiedCardinality",
            "onClass",
            "onDataRange",
            "equivalentClass",
            "disjointWith",
            "disjointUnionOf",
            "equivalentProperty",
            "propertyDisjointWith",
            "inverseOf",
            "propertyChainAxiom",
            "hasKey",
            "sameAs",
            "differentFrom",
            "members",
            "distinctMembers",
            "sourceIndividual",
            "assertionProperty",
            "targetIndividual",
            "targetValue",
            "annotatedSource",
            "annotatedProperty",
            "annotatedTarget",
        )
    },
}


def _is_structural_type(value: str) -> bool:
    return value.startswith(OWL) or value in {
        RDF + "List",
        RDF + "Property",
        RDFS + "Class",
        RDFS + "Datatype",
    }


def _resource_key(value: RDFResource) -> tuple[str, str]:
    return ("I", value.value) if isinstance(value, RDFIRI) else ("B", value.label)


def _term_key(value: RDFTerm) -> tuple[str, str, str]:
    if isinstance(value, RDFIRI):
        return "I", value.value, ""
    if isinstance(value, RDFBlank):
        return "B", value.label, ""
    return "L", value.lexical, (value.language or "") + "\x00" + (value.datatype or "")


def _term_text(value: RDFTerm) -> str:
    if isinstance(value, RDFIRI):
        return f"<{value.value}>"
    if isinstance(value, RDFBlank):
        return "_:" + value.label
    return repr(value.lexical)


def _evidence(value: Triple) -> RDFTripleEvidence:
    return RDFTripleEvidence(
        _term_text(value.subject), value.predicate.value, _term_text(value.object)
    )


__all__ = [
    "OWL",
    "RDF",
    "RDFIRI",
    "RDFS",
    "XSD",
    "RDFBlank",
    "RDFEncoder",
    "RDFGraph",
    "RDFLiteral",
    "RDFMapper",
    "RDFResource",
    "RDFTerm",
    "Triple",
]
