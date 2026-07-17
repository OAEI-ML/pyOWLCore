"""Pure-Python OWL 2 Functional-Style Syntax reader and writer."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar, cast

import pyowl_core.model as m
from pyowl_core.cancellation import CancellationToken
from pyowl_core.diagnostics import Diagnostic, Severity, SourceSpan
from pyowl_core.document import OntologyDocument, OntologyID
from pyowl_core.document.document import provisional_anonymous
from pyowl_core.exceptions import OntologySyntaxError, ResourceLimitError, UnsupportedSyntaxError
from pyowl_core.limits import ParseLimits
from pyowl_core.model.swrl import Atom, DataArgument, IndividualArgument, SWRLRule, Variable

from .common import ParseContext, ParsedOntology

_BUILTIN_PREFIXES = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}
_DELIMITERS = frozenset("()=<>@^\"' \t\r\n")
_LANGUAGE = re.compile(r"@[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_UNICODE_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    line: int
    column: int

    def span(self) -> SourceSpan:
        return SourceSpan(
            byte_start=self.byte_start,
            byte_end=self.byte_end,
            line_start=self.line,
            column_start=self.column,
        )


def parse_functional(
    data: bytes,
    *,
    limits: ParseLimits,
    cancellation_token: CancellationToken | None = None,
    allow_swrl: bool = False,
) -> ParsedOntology:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise OntologySyntaxError(
            "Functional-Style source must be valid UTF-8",
            code="FUNCTIONAL_ENCODING",
        ) from error
    parser = FunctionalParser(text, limits, cancellation_token, allow_swrl=allow_swrl)
    return parser.parse()


class FunctionalLexer:
    __slots__ = ("context", "length", "text")

    def __init__(self, text: str, context: ParseContext) -> None:
        self.text = text
        self.length = len(text)
        self.context = context

    def tokenize(self) -> tuple[Token, ...]:
        tokens: list[Token] = []
        index = 0
        byte_offset = 0
        line = 1
        column = 1
        while index < self.length:
            self.context.check()
            character = self.text[index]
            if character in " \t\r\n":
                whitespace_width = (
                    2
                    if character == "\r"
                    and index + 1 < self.length
                    and self.text[index + 1] == "\n"
                    else 1
                )
                index, byte_offset, line, column = self._advance(
                    index, byte_offset, line, column, whitespace_width
                )
                continue
            if character == "#":
                end = index
                while end < self.length and self.text[end] not in "\r\n":
                    end += 1
                index, byte_offset, line, column = self._advance(
                    index, byte_offset, line, column, end - index
                )
                continue
            start = index
            start_byte = byte_offset
            start_line = line
            start_column = column
            if self.text.startswith("^^", index):
                kind, value, width = "HAT", "^^", 2
            elif character in "()=":
                kind, value, width = character, character, 1
            elif character == "<":
                end = index + 1
                escaped = False
                while end < self.length:
                    current = self.text[end]
                    if current == ">" and not escaped:
                        break
                    if current in "\r\n":
                        self._raise("IRI reference cannot contain a line break", start, end)
                    escaped = current == "\\" and not escaped
                    if current != "\\":
                        escaped = False
                    end += 1
                if end >= self.length:
                    self._raise("unterminated IRI reference", start, self.length)
                kind, value, width = "IRI", self.text[index + 1 : end], end - index + 1
            elif character == '"':
                end = index + 1
                value_parts: list[str] = []
                while end < self.length:
                    current = self.text[end]
                    if current == '"':
                        break
                    if current == "\\":
                        if end + 1 >= self.length or self.text[end + 1] not in {'"', "\\"}:
                            self._raise(
                                "invalid quoted-string escape", end, min(end + 2, self.length)
                            )
                        value_parts.append(self.text[end + 1])
                        end += 2
                        continue
                    value_parts.append(current)
                    end += 1
                if end >= self.length:
                    self._raise("unterminated quoted string", start, self.length)
                kind, value, width = "STRING", "".join(value_parts), end - index + 1
                if len(value.encode("utf-8")) > self.context.limits.max_literal_bytes:
                    raise ResourceLimitError(
                        "resource limit max_literal_bytes exceeded",
                        limit="max_literal_bytes",
                        observed=len(value.encode("utf-8")),
                        allowed=self.context.limits.max_literal_bytes,
                    )
            elif character == "@":
                match = _LANGUAGE.match(self.text, index)
                if match is None:
                    self._raise("invalid language tag", start, start + 1)
                kind, value, width = "LANG", match.group()[1:], match.end() - index
            else:
                end = index
                while end < self.length and self.text[end] not in _DELIMITERS:
                    end += 1
                if end == index:
                    self._raise("unexpected character", start, start + 1)
                value = self.text[index:end]
                kind = "INTEGER" if value.isdigit() else "WORD"
                width = end - index
            index, byte_offset, line, column = self._advance(
                index, byte_offset, line, column, width
            )
            tokens.append(
                Token(
                    kind,
                    value,
                    start,
                    index,
                    start_byte,
                    byte_offset,
                    start_line,
                    start_column,
                )
            )
        tokens.append(Token("EOF", "", index, index, byte_offset, byte_offset, line, column))
        return tuple(tokens)

    def _advance(
        self, index: int, byte_offset: int, line: int, column: int, width: int
    ) -> tuple[int, int, int, int]:
        fragment = self.text[index : index + width]
        byte_offset += len(fragment.encode("utf-8"))
        position = 0
        while position < len(fragment):
            character = fragment[position]
            if character == "\r":
                line += 1
                column = 1
                if position + 1 < len(fragment) and fragment[position + 1] == "\n":
                    position += 1
            elif character == "\n":
                line += 1
                column = 1
            else:
                column += 1
            position += 1
        return index + width, byte_offset, line, column

    def _raise(self, message: str, start: int, end: int) -> NoReturn:
        prefix = self.text[:start]
        line = prefix.count("\n") + 1
        last = prefix.rfind("\n")
        column = start + 1 if last < 0 else start - last
        span = SourceSpan(
            byte_start=len(prefix.encode("utf-8")),
            byte_end=len(self.text[:end].encode("utf-8")),
            line_start=line,
            column_start=column,
        )
        diagnostic = Diagnostic(
            code="FUNCTIONAL_LEXICAL",
            severity=Severity.ERROR,
            message=message,
            source_span=span,
            details={"rule": "OWL2-FSS-LEXER"},
        )
        raise OntologySyntaxError(message, diagnostic=diagnostic)


class FunctionalParser:
    __slots__ = (
        "allow_swrl",
        "context",
        "depth",
        "index",
        "occurrences",
        "prefixes",
        "text",
        "tokens",
    )

    def __init__(
        self,
        text: str,
        limits: ParseLimits,
        cancellation_token: CancellationToken | None,
        *,
        allow_swrl: bool,
    ) -> None:
        self.text = text
        self.context = ParseContext(limits, cancellation_token)
        self.tokens = FunctionalLexer(text, self.context).tokenize()
        self.index = 0
        self.depth = 0
        self.allow_swrl = allow_swrl
        self.prefixes = dict(_BUILTIN_PREFIXES)
        self.occurrences: list[tuple[m.StructuralNode, SourceSpan | None]] = []

    def parse(self) -> ParsedOntology:
        while self._word("Prefix"):
            self._parse_prefix()
        self._expect_word("Ontology")
        self._open()
        ontology_iri: m.IRI | None = None
        version_iri: m.IRI | None = None
        if self._starts_iri() and not self._word_any(
            "Import", "Annotation", *self._axiom_names(), "SWRLRule"
        ):
            ontology_iri = self._parse_iri()
            if self._starts_iri() and not self._word_any(
                "Import", "Annotation", *self._axiom_names(), "SWRLRule"
            ):
                version_iri = self._parse_iri()
        imports: list[m.IRI] = []
        annotations: list[m.Annotation] = []
        axioms: list[m.AxiomNode] = []
        extensions: list[m.StructuralNode] = []
        while not self._at(")"):
            self.context.check()
            start = self._peek()
            if self._word("Import"):
                self._take()
                self._open()
                imports.append(self._parse_iri())
                self._close()
            elif self._word("Annotation"):
                annotation = self._parse_annotation()
                annotations.append(annotation)
                self.occurrences.append((annotation, self._span_from(start)))
            elif self._word("SWRLRule"):
                if not self.allow_swrl:
                    self._unsupported("SWRL extension requires explicit enablement")
                rule = self._parse_swrl_rule()
                extensions.append(rule)
                self.occurrences.append((rule, self._span_from(start)))
            else:
                axiom = self._parse_axiom()
                axioms.append(axiom)
                self.context.limits.enforce("max_axioms", len(axioms))
                self.occurrences.append((axiom, self._span_from(start)))
        self._close()
        self._expect("EOF")
        return ParsedOntology(
            OntologyID(ontology_iri, version_iri),
            tuple(imports),
            tuple(annotations),
            tuple(axioms),
            tuple(extensions),
            tuple(sorted(self.prefixes.items())),
            tuple(self.occurrences),
            decoded_codepoint_length=len(self.text),
        )

    def _parse_prefix(self) -> None:
        self._expect_word("Prefix")
        self._open()
        token = self._expect("WORD")
        if not token.value.endswith(":"):
            self._syntax("prefix name must end in ':'", token)
        prefix = token.value[:-1]
        self._expect("=")
        iri_token = self._expect("IRI")
        iri = _decode_iri_escapes(iri_token.value, self._syntax_at)
        self.context.limits.enforce("max_iri_bytes", len(iri.encode("utf-8")))
        self.prefixes[prefix] = iri
        self.context.limits.enforce("max_prefixes", len(self.prefixes))
        self._close()

    def _parse_axiom(self) -> m.AxiomNode:
        name = self._expect("WORD").value
        if name not in self._axiom_names():
            self._syntax(f"unknown axiom constructor {name!r}", self.tokens[self.index - 1])
        self._open()
        annotations = self._parse_annotations()
        if name == "Declaration":
            value: m.AxiomNode = m.Declaration(self._parse_entity(), annotations)
        elif name == "SubClassOf":
            value = m.SubClassOf(
                self._parse_class_expression(), self._parse_class_expression(), annotations
            )
        elif name == "EquivalentClasses":
            expressions = self._many(self._parse_class_expression)
            value = m.EquivalentClasses(m.CanonicalSet(expressions), annotations)
        elif name == "DisjointClasses":
            expressions = self._many(self._parse_class_expression)
            value = _disjoint_classes(expressions, annotations)
        elif name == "DisjointUnion":
            value = m.DisjointUnion(
                self._parse_class(),
                m.CanonicalSet(self._many(self._parse_class_expression)),
                annotations,
            )
        elif name == "SubObjectPropertyOf":
            value = m.SubObjectPropertyOf(
                self._parse_sub_object_property(), self._parse_object_property(), annotations
            )
        elif name in {"EquivalentObjectProperties", "DisjointObjectProperties"}:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(
                    m.CanonicalSet(self._many(self._parse_object_property)), annotations
                ),
            )
        elif name == "InverseObjectProperties":
            value = m.InverseObjectProperties(
                self._parse_object_property(), self._parse_object_property(), annotations
            )
        elif name in {"ObjectPropertyDomain", "ObjectPropertyRange"}:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(
                    self._parse_object_property(),
                    self._parse_class_expression(),
                    annotations,
                ),
            )
        elif name in _OBJECT_CHARACTERISTICS:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(self._parse_object_property(), annotations),
            )
        elif name == "SubDataPropertyOf":
            value = m.SubDataPropertyOf(
                self._parse_data_property(), self._parse_data_property(), annotations
            )
        elif name in {"EquivalentDataProperties", "DisjointDataProperties"}:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(
                    m.CanonicalSet(self._many(self._parse_data_property)), annotations
                ),
            )
        elif name == "DataPropertyDomain":
            value = m.DataPropertyDomain(
                self._parse_data_property(), self._parse_class_expression(), annotations
            )
        elif name == "DataPropertyRange":
            value = m.DataPropertyRange(
                self._parse_data_property(), self._parse_data_range(), annotations
            )
        elif name == "FunctionalDataProperty":
            value = m.FunctionalDataProperty(self._parse_data_property(), annotations)
        elif name == "DatatypeDefinition":
            value = m.DatatypeDefinition(
                self._parse_datatype(), self._parse_data_range(), annotations
            )
        elif name == "HasKey":
            expression = self._parse_class_expression()
            self._open()
            object_properties = self._many(self._parse_object_property)
            self._close()
            self._open()
            data_properties = self._many(self._parse_data_property)
            self._close()
            value = m.HasKey(
                expression,
                m.CanonicalSet(object_properties),
                m.CanonicalSet(data_properties),
                annotations,
            )
        elif name in {"SameIndividual", "DifferentIndividuals"}:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(m.CanonicalSet(self._many(self._parse_individual)), annotations),
            )
        elif name == "ClassAssertion":
            value = m.ClassAssertion(
                self._parse_class_expression(), self._parse_individual(), annotations
            )
        elif name in {"ObjectPropertyAssertion", "NegativeObjectPropertyAssertion"}:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(
                    self._parse_object_property(),
                    self._parse_individual(),
                    self._parse_individual(),
                    annotations,
                ),
            )
        elif name in {"DataPropertyAssertion", "NegativeDataPropertyAssertion"}:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(
                    self._parse_data_property(),
                    self._parse_individual(),
                    self._parse_literal(),
                    annotations,
                ),
            )
        elif name == "AnnotationAssertion":
            value = m.AnnotationAssertion(
                self._parse_annotation_property(),
                self._parse_annotation_subject(),
                self._parse_annotation_value(),
                annotations,
            )
        elif name == "SubAnnotationPropertyOf":
            value = m.SubAnnotationPropertyOf(
                self._parse_annotation_property(),
                self._parse_annotation_property(),
                annotations,
            )
        elif name in {"AnnotationPropertyDomain", "AnnotationPropertyRange"}:
            value = cast(
                m.AxiomNode,
                getattr(m, name)(self._parse_annotation_property(), self._parse_iri(), annotations),
            )
        else:
            raise AssertionError(name)
        self._close()
        return value

    def _parse_entity(self) -> m.Entity:
        token = self._expect("WORD")
        constructors: dict[str, Callable[[m.IRI], m.Entity]] = {
            "Class": m.Class,
            "Datatype": m.Datatype,
            "ObjectProperty": m.ObjectProperty,
            "DataProperty": m.DataProperty,
            "AnnotationProperty": m.AnnotationProperty,
            "NamedIndividual": m.NamedIndividual,
        }
        constructor = constructors.get(token.value)
        if constructor is None:
            self._syntax("expected an entity constructor", token)
        self._open()
        value = constructor(self._parse_iri())
        self._close()
        return value

    def _parse_annotation(self) -> m.Annotation:
        self._expect_word("Annotation")
        self._open()
        annotations = self._parse_annotations()
        value = m.Annotation(
            self._parse_annotation_property(), self._parse_annotation_value(), annotations
        )
        self._close()
        return value

    def _parse_annotations(self) -> m.CanonicalSet[m.Annotation]:
        values: list[m.Annotation] = []
        while self._word("Annotation"):
            values.append(self._parse_annotation())
            self.context.limits.enforce("max_annotations", len(values))
        return m.CanonicalSet(values)

    def _parse_class_expression(self) -> m.ClassExpression:
        name = self._call_name()
        if name not in _CLASS_EXPRESSION_CONSTRUCTORS:
            return self._parse_class()
        self._take()
        self._open()
        if name in {"ObjectIntersectionOf", "ObjectUnionOf"}:
            operands = self._many(self._parse_class_expression)
            canonical_operands = m.CanonicalSet(operands)
            if len(operands) >= 2 and len(canonical_operands) == 1:
                value: m.ClassExpression = next(iter(canonical_operands))
            else:
                value = cast(
                    m.ClassExpression,
                    getattr(m, name)(canonical_operands),
                )
        elif name == "ObjectComplementOf":
            value = m.ObjectComplementOf(self._parse_class_expression())
        elif name == "ObjectOneOf":
            value = m.ObjectOneOf(m.CanonicalSet(self._many(self._parse_individual)))
        elif name in {"ObjectSomeValuesFrom", "ObjectAllValuesFrom"}:
            value = cast(
                m.ClassExpression,
                getattr(m, name)(self._parse_object_property(), self._parse_class_expression()),
            )
        elif name == "ObjectHasValue":
            value = m.ObjectHasValue(self._parse_object_property(), self._parse_individual())
        elif name == "ObjectHasSelf":
            value = m.ObjectHasSelf(self._parse_object_property())
        elif name in _OBJECT_CARDINALITIES:
            cardinality = self._parse_integer()
            object_property = self._parse_object_property()
            class_filler = m.OWL_THING if self._at(")") else self._parse_class_expression()
            value = cast(
                m.ClassExpression,
                getattr(m, name)(cardinality, object_property, class_filler),
            )
        elif name in {"DataSomeValuesFrom", "DataAllValuesFrom"}:
            properties, data_filler = self._parse_data_quantified_arguments()
            value = cast(m.ClassExpression, getattr(m, name)(properties, data_filler))
        elif name == "DataHasValue":
            value = m.DataHasValue(self._parse_data_property(), self._parse_literal())
        elif name in _DATA_CARDINALITIES:
            cardinality = self._parse_integer()
            data_property = self._parse_data_property()
            range_filler = m.RDFS_LITERAL if self._at(")") else self._parse_data_range()
            value = cast(
                m.ClassExpression,
                getattr(m, name)(cardinality, data_property, range_filler),
            )
        else:
            self._syntax(f"unknown class expression {name!r}", self.tokens[self.index - 2])
        self._close()
        return value

    def _parse_data_quantified_arguments(
        self,
    ) -> tuple[tuple[m.DataProperty, ...], m.DataRange]:
        properties: list[m.DataProperty] = []
        while not self._at(")"):
            if self._call_name() in _DATA_RANGE_CONSTRUCTORS:
                if not properties:
                    self._syntax("data restriction requires a property", self._peek())
                return tuple(properties), self._parse_data_range()
            iri = self._parse_iri()
            if self._at(")"):
                if not properties:
                    self._syntax("data restriction requires a property and filler", self._peek())
                return tuple(properties), m.Datatype(iri)
            properties.append(m.DataProperty(iri))
        self._syntax("data restriction requires a filler", self._peek())

    def _parse_data_range(self) -> m.DataRange:
        name = self._call_name()
        if name is None:
            return self._parse_datatype()
        self._take()
        self._open()
        if name in {"DataIntersectionOf", "DataUnionOf"}:
            operands = self._many(self._parse_data_range)
            canonical_operands = m.CanonicalSet(operands)
            if len(operands) >= 2 and len(canonical_operands) == 1:
                value: m.DataRange = next(iter(canonical_operands))
            else:
                value = cast(
                    m.DataRange,
                    getattr(m, name)(canonical_operands),
                )
        elif name == "DataComplementOf":
            value = m.DataComplementOf(self._parse_data_range())
        elif name == "DataOneOf":
            value = m.DataOneOf(m.CanonicalSet(self._many(self._parse_literal)))
        elif name == "DatatypeRestriction":
            value = m.DatatypeRestriction(
                self._parse_datatype(),
                m.CanonicalSet(self._many(self._parse_facet_restriction)),
            )
        else:
            self._syntax(f"unknown data range {name!r}", self.tokens[self.index - 2])
        self._close()
        return value

    def _parse_facet_restriction(self) -> m.FacetRestriction:
        return m.FacetRestriction(self._parse_iri(), self._parse_literal())

    def _parse_sub_object_property(self) -> m.SubObjectPropertyExpression:
        if self._word("ObjectPropertyChain"):
            self._take()
            self._open()
            value = m.ObjectPropertyChain(self._many(self._parse_object_property))
            self._close()
            return value
        return self._parse_object_property()

    def _parse_object_property(self) -> m.ObjectPropertyExpression:
        if self._word("ObjectInverseOf"):
            self._take()
            self._open()
            value = m.ObjectInverseOf(m.ObjectProperty(self._parse_iri()))
            self._close()
            return value
        return m.ObjectProperty(self._parse_iri())

    def _parse_data_property(self) -> m.DataProperty:
        return m.DataProperty(self._parse_iri())

    def _parse_annotation_property(self) -> m.AnnotationProperty:
        return m.AnnotationProperty(self._parse_iri())

    def _parse_class(self) -> m.Class:
        return m.Class(self._parse_iri())

    def _parse_datatype(self) -> m.Datatype:
        return m.Datatype(self._parse_iri())

    def _parse_individual(self) -> m.Individual:
        if self._peek().kind == "WORD" and self._peek().value.startswith("_:"):
            return provisional_anonymous(self._take().value[2:])
        return m.NamedIndividual(self._parse_iri())

    def _parse_annotation_subject(self) -> m.AnnotationSubject:
        if self._peek().kind == "WORD" and self._peek().value.startswith("_:"):
            return provisional_anonymous(self._take().value[2:])
        return self._parse_iri()

    def _parse_annotation_value(self) -> m.AnnotationValue:
        if self._peek().kind == "STRING":
            return self._parse_literal()
        if self._peek().kind == "WORD" and self._peek().value.startswith("_:"):
            return provisional_anonymous(self._take().value[2:])
        return self._parse_iri()

    def _parse_literal(self) -> m.Literal:
        token = self._expect("STRING")
        if self._peek().kind == "LANG":
            language = self._take().value
            return m.Literal(token.value, m.RDF_PLAIN_LITERAL, language)
        datatype = m.RDF_PLAIN_LITERAL
        if self._peek().kind == "HAT":
            self._take()
            datatype = m.Datatype(self._parse_iri())
        return m.Literal(token.value, datatype)

    def _parse_iri(self) -> m.IRI:
        token = self._peek()
        if token.kind == "IRI":
            self._take()
            value = _decode_iri_escapes(token.value, self._syntax_at)
        elif token.kind == "WORD" and ":" in token.value and not token.value.startswith("_:"):
            self._take()
            prefix, local = token.value.split(":", 1)
            try:
                base = self.prefixes[prefix]
            except KeyError:
                self._syntax(f"undefined prefix {prefix!r}", token)
            value = base + _decode_pname(local, self._syntax_at)
        else:
            self._syntax("expected an IRI", token)
        encoded = value.encode("utf-8")
        self.context.limits.enforce("max_iri_bytes", len(encoded))
        try:
            return m.IRI(value)
        except Exception as error:
            self._syntax(str(error), token, cause=error)

    def _parse_integer(self) -> int:
        token = self._expect("INTEGER")
        return int(token.value)

    def _parse_swrl_rule(self) -> m.StructuralNode:
        from pyowl_core.extensions import swrl

        self._expect_word("SWRLRule")
        self._open()
        annotations = self._parse_annotations()
        self._open()
        body = self._many(self._parse_swrl_atom)
        self._close()
        self._open()
        head = self._many(self._parse_swrl_atom)
        self._close()
        self._close()
        return swrl.SWRLRule(m.CanonicalSet(body), m.CanonicalSet(head), annotations)

    def _parse_swrl_atom(self) -> Atom:
        from pyowl_core.extensions import swrl

        name = self._expect("WORD").value
        self._open()
        if name == "ClassAtom":
            value: Atom = swrl.ClassAtom(self._parse_class_expression(), self._parse_swrl_iarg())
        elif name == "DataRangeAtom":
            value = swrl.DataRangeAtom(self._parse_data_range(), self._parse_swrl_darg())
        elif name == "ObjectPropertyAtom":
            value = swrl.ObjectPropertyAtom(
                self._parse_object_property(), self._parse_swrl_iarg(), self._parse_swrl_iarg()
            )
        elif name == "DataPropertyAtom":
            value = swrl.DataPropertyAtom(
                self._parse_data_property(), self._parse_swrl_iarg(), self._parse_swrl_darg()
            )
        elif name == "BuiltInAtom":
            value = swrl.BuiltInAtom(self._parse_iri(), self._many(self._parse_swrl_darg))
        elif name in {"SameIndividualAtom", "DifferentIndividualsAtom"}:
            value = cast(
                Atom,
                getattr(swrl, name)(self._parse_swrl_iarg(), self._parse_swrl_iarg()),
            )
        else:
            self._syntax(f"unknown SWRL atom {name!r}", self.tokens[self.index - 2])
        self._close()
        return value

    def _parse_swrl_iarg(self) -> IndividualArgument:
        if self._word("Variable"):
            return self._parse_variable()
        return self._parse_individual()

    def _parse_swrl_darg(self) -> DataArgument:
        if self._word("Variable"):
            return self._parse_variable()
        return self._parse_literal()

    def _parse_variable(self) -> Variable:
        from pyowl_core.extensions.swrl import Variable as PublicVariable

        self._expect_word("Variable")
        self._open()
        value = PublicVariable(self._parse_iri())
        self._close()
        return value

    def _many(self, parser: Callable[[], T]) -> tuple[T, ...]:
        values: list[T] = []
        while not self._at(")"):
            values.append(parser())
            self.context.limits.enforce("max_sequence_arity", len(values))
        return tuple(values)

    def _call_name(self) -> str | None:
        if self._peek().kind == "WORD" and self.tokens[self.index + 1].kind == "(":
            return self._peek().value
        return None

    def _open(self) -> None:
        self._expect("(")
        self.depth += 1
        self.context.depth(self.depth)

    def _close(self) -> None:
        self._expect(")")
        self.depth -= 1

    def _starts_iri(self) -> bool:
        token = self._peek()
        return token.kind == "IRI" or (
            token.kind == "WORD" and ":" in token.value and not token.value.startswith("_:")
        )

    def _word(self, value: str) -> bool:
        return self._peek().kind == "WORD" and self._peek().value == value

    def _word_any(self, *values: str) -> bool:
        return self._peek().kind == "WORD" and self._peek().value in values

    def _at(self, kind: str) -> bool:
        return self._peek().kind == kind

    def _peek(self) -> Token:
        return self.tokens[self.index]

    def _take(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        self.context.check()
        return token

    def _expect(self, kind: str) -> Token:
        token = self._peek()
        if token.kind != kind:
            self._syntax(f"expected {kind!r}, found {token.value or token.kind!r}", token)
        return self._take()

    def _expect_word(self, value: str) -> Token:
        token = self._expect("WORD")
        if token.value != value:
            self._syntax(f"expected {value!r}, found {token.value!r}", token)
        return token

    def _span_from(self, start: Token) -> SourceSpan:
        end = self.tokens[max(0, self.index - 1)]
        return SourceSpan(
            byte_start=start.byte_start,
            byte_end=end.byte_end,
            line_start=start.line,
            column_start=start.column,
        )

    def _syntax_at(self, message: str) -> NoReturn:
        self._syntax(message, self._peek())

    def _syntax(
        self, message: str, token: Token, *, cause: BaseException | None = None
    ) -> NoReturn:
        diagnostic = Diagnostic(
            code="FUNCTIONAL_SYNTAX",
            severity=Severity.ERROR,
            message=message,
            source_span=token.span(),
            details={"rule": "OWL2-FSS"},
        )
        error = OntologySyntaxError(message, diagnostic=diagnostic)
        if cause is None:
            raise error
        raise error from cause

    def _unsupported(self, message: str) -> NoReturn:
        raise UnsupportedSyntaxError(message, code="FUNCTIONAL_EXTENSION_DISABLED")

    @staticmethod
    def _axiom_names() -> frozenset[str]:
        return _AXIOM_NAMES


def _disjoint_classes(
    expressions: tuple[m.ClassExpression, ...],
    annotations: m.CanonicalSet[m.Annotation],
) -> m.AxiomNode:
    canonical = m.CanonicalSet(expressions)
    if len(expressions) >= 2 and len(canonical) == 1:
        return m.SubClassOf(next(iter(canonical)), m.OWL_NOTHING, annotations)
    return m.DisjointClasses(canonical, annotations)


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
_CLASS_EXPRESSION_CONSTRUCTORS = frozenset(
    {
        "ObjectIntersectionOf",
        "ObjectUnionOf",
        "ObjectComplementOf",
        "ObjectOneOf",
        "ObjectSomeValuesFrom",
        "ObjectAllValuesFrom",
        "ObjectHasValue",
        "ObjectHasSelf",
        "DataSomeValuesFrom",
        "DataAllValuesFrom",
        "DataHasValue",
        *_OBJECT_CARDINALITIES,
        *_DATA_CARDINALITIES,
    }
)
_DATA_RANGE_CONSTRUCTORS = frozenset(
    {"DataIntersectionOf", "DataUnionOf", "DataComplementOf", "DataOneOf", "DatatypeRestriction"}
)
_AXIOM_NAMES = frozenset(
    spec.constructor.__name__
    for spec in m.CONSTRUCTOR_SPECS
    if spec.category in {"declaration_axiom", "logical_axiom", "annotation_axiom"}
)


def render_functional(document: OntologyDocument) -> bytes:
    if not isinstance(document, OntologyDocument):
        raise TypeError("document must be OntologyDocument")
    lines = ["Ontology("]
    if document.ontology_id.ontology_iri is not None:
        identity = "  " + _render_iri(document.ontology_id.ontology_iri)
        if document.ontology_id.version_iri is not None:
            identity += " " + _render_iri(document.ontology_id.version_iri)
        lines.append(identity)
    lines.extend(f"  Import({_render_iri(item)})" for item in document.direct_imports)
    lines.extend(f"  {_render_node(item)}" for item in document.ontology_annotations)
    lines.extend(f"  {_render_node(item)}" for item in document.axioms)
    lines.extend(f"  {_render_node(item)}" for item in document.extension_components)
    lines.append(")")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_node(value: object) -> str:
    if isinstance(value, m.IRI):
        return _render_iri(value)
    if isinstance(value, m.Entity):
        return _render_iri(value.iri)
    if isinstance(value, m.AnonymousIndividual):
        return "_:b" + value.local_key.hex()
    if isinstance(value, m.Literal):
        lexical = value.lexical_form.replace("\\", "\\\\").replace('"', '\\"')
        if value.language is not None:
            return f'"{lexical}"@{value.language}'
        if value.datatype == m.RDF_PLAIN_LITERAL:
            return f'"{lexical}"'
        return f'"{lexical}"^^{_render_iri(value.datatype.iri)}'
    if isinstance(value, m.Annotation):
        members = [*value.annotations, value.property, value.value]
        return "Annotation(" + " ".join(_render_node(item) for item in members) + ")"
    if isinstance(value, m.ObjectPropertyChain):
        return (
            "ObjectPropertyChain(" + " ".join(_render_node(item) for item in value.properties) + ")"
        )
    if isinstance(value, m.DatatypeRestriction):
        restrictions = " ".join(
            f"{_render_iri(item.facet)} {_render_node(item.value)}" for item in value.restrictions
        )
        return "DatatypeRestriction(" + _render_node(value.datatype) + " " + restrictions + ")"
    if isinstance(value, m.StructuralNode):
        name = type(value).__name__
        if isinstance(value, m.Declaration):
            entity = value.entity
            declaration_members: list[object] = [
                *value.annotations,
                f"{type(entity).__name__}({_render_iri(entity.iri)})",
            ]
            return (
                "Declaration("
                + " ".join(_render_node_or_text(item) for item in declaration_members)
                + ")"
            )
        if isinstance(value, m.HasKey):
            key_members: list[object] = [*value.annotations, value.class_expression]
            objects = "(" + " ".join(_render_node(item) for item in value.object_properties) + ")"
            data = "(" + " ".join(_render_node(item) for item in value.data_properties) + ")"
            return (
                f"HasKey({' '.join(_render_node(item) for item in key_members)} {objects} {data})"
            )
        if isinstance(value, SWRLRule):
            body = "(" + " ".join(_render_node(item) for item in value.body) + ")"
            head = "(" + " ".join(_render_node(item) for item in value.head) + ")"
            annotations = " ".join(_render_node(item) for item in value.annotations)
            prefix = annotations + " " if annotations else ""
            return f"SWRLRule({prefix}{body} {head})"
        spec = m.constructor_spec(value)
        rendered_members: list[object] = []
        node = cast(Any, value)
        node_annotations = getattr(node, "annotations", None)
        if isinstance(node_annotations, m.CanonicalSet):
            rendered_members.extend(node_annotations)
        for field in spec.fields:
            if field == "annotations":
                continue
            item = getattr(node, field)
            if isinstance(item, (m.CanonicalSet, tuple)):
                rendered_members.extend(item)
            else:
                rendered_members.append(item)
        return f"{name}({' '.join(_render_node_or_integer(item) for item in rendered_members)})"
    raise TypeError(f"cannot render {type(value).__name__} in Functional Syntax")


def _render_node_or_text(value: object) -> str:
    return value if isinstance(value, str) else _render_node(value)


def _render_node_or_integer(value: object) -> str:
    return str(value) if isinstance(value, int) else _render_node(value)


def _render_iri(value: m.IRI) -> str:
    return "<" + value.value.replace("\\", "\\\\").replace(">", "\\u003E") + ">"


def _decode_iri_escapes(value: str, fail: Callable[[str], NoReturn]) -> str:
    def replace(match: re.Match[str]) -> str:
        codepoint = int(match.group(1) or match.group(2), 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            fail("IRI contains an invalid Unicode escape")
        return chr(codepoint)

    if "\\" not in value:
        return value
    replaced = _UNICODE_ESCAPE.sub(replace, value)
    if "\\" in replaced:
        fail("IRI contains an unsupported escape")
    return replaced


def _decode_pname(value: str, fail: Callable[[str], NoReturn]) -> str:
    value = _decode_iri_escapes(value, fail)
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\":
            if index + 1 >= len(value):
                fail("truncated prefixed-name escape")
            output.append(value[index + 1])
            index += 2
        else:
            output.append(value[index])
            index += 1
    return "".join(output)


__all__ = ["FunctionalLexer", "FunctionalParser", "parse_functional", "render_functional"]
