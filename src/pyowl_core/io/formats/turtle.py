"""Bounded RDF 1.1 Turtle reader and deterministic OWL Turtle writer."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urljoin

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import OntologyDocument
from pyowl_core.exceptions import OntologySyntaxError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI

from .common import ParseContext, ParsedOntology
from .rdf import (
    RDF,
    RDFIRI,
    XSD,
    RDFBlank,
    RDFEncoder,
    RDFGraph,
    RDFLiteral,
    RDFMapper,
    RDFResource,
    RDFTerm,
    Triple,
)

_NUMBER = re.compile(
    r"[+-]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+|[0-9]+)"
)
_LANG = re.compile(r"@[A-Za-z]+(?:-[A-Za-z0-9]+)*")
_UCHAR = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")
_WORD_STOP = frozenset(" \t\r\n;,[]()<>\"'^")
_PN_LOCAL_ESCAPES = frozenset("_~.-!$&'()*+,;=/?#@%")


@dataclass(frozen=True, slots=True)
class TurtleToken:
    kind: str
    value: str
    offset: int
    line: int
    column: int


def parse_turtle(
    data: bytes,
    *,
    limits: ParseLimits,
    document_iri: IRI | None,
    cancellation_token: CancellationToken | None = None,
    allow_partial_rdf_mapping: bool = False,
    allow_swrl: bool = False,
) -> ParsedOntology:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise OntologySyntaxError(
            "Turtle source must be valid UTF-8", code="TURTLE_ENCODING"
        ) from error
    parser = TurtleParser(text, limits, document_iri, cancellation_token)
    graph, prefixes = parser.parse()
    mapped = RDFMapper(
        graph,
        limits=limits,
        document_iri=document_iri,
        cancellation_token=cancellation_token,
        allow_swrl=allow_swrl,
    ).map(allow_partial=allow_partial_rdf_mapping)
    return ParsedOntology(
        mapped.ontology_id,
        mapped.imports,
        mapped.annotations,
        mapped.axioms,
        mapped.extensions,
        tuple(sorted(prefixes.items())),
        mapped.occurrences,
        mapped.rdf_mapping_report,
        len(text),
    )


class TurtleLexer:
    __slots__ = ("context", "length", "text")

    def __init__(self, text: str, context: ParseContext) -> None:
        self.text = text
        self.length = len(text)
        self.context = context

    def tokenize(self) -> tuple[TurtleToken, ...]:
        output: list[TurtleToken] = []
        index = 0
        line = 1
        column = 1
        while index < self.length:
            self.context.check()
            char = self.text[index]
            if char in " \t\r\n":
                whitespace_width = (
                    2
                    if char == "\r" and index + 1 < self.length and self.text[index + 1] == "\n"
                    else 1
                )
                index, line, column = _advance(self.text, index, whitespace_width, line, column)
                continue
            if char == "#":
                end = index
                while end < self.length and self.text[end] not in "\r\n":
                    end += 1
                index, line, column = _advance(self.text, index, end - index, line, column)
                continue
            start, start_line, start_column = index, line, column
            if self.text.startswith("^^", index):
                kind, value, width = "HAT", "^^", 2
            elif char in ".;,[]()":
                kind, value, width = char, char, 1
            elif char == "<":
                end = index + 1
                while end < self.length and self.text[end] != ">":
                    if self.text[end] in "\r\n":
                        self._syntax("IRIREF cannot contain a line break", start_line, start_column)
                    end += 1
                if end >= self.length:
                    self._syntax("unterminated IRIREF", start_line, start_column)
                kind, value, width = "IRI", self.text[index + 1 : end], end - index + 1
            elif char in {'"', "'"}:
                quote = char
                long = self.text.startswith(quote * 3, index)
                delimiter = quote * (3 if long else 1)
                end = index + len(delimiter)
                parts: list[str] = []
                while end < self.length and not self.text.startswith(delimiter, end):
                    current = self.text[end]
                    if not long and current in "\r\n":
                        self._syntax(
                            "short Turtle string cannot span lines", start_line, start_column
                        )
                    if current == "\\":
                        decoded, width_escape = self._escape(end)
                        parts.append(decoded)
                        end += width_escape
                    else:
                        parts.append(current)
                        end += 1
                if end >= self.length:
                    self._syntax("unterminated Turtle string", start_line, start_column)
                value = "".join(parts)
                encoded_length = len(value.encode("utf-8"))
                self.context.limits.enforce("max_literal_bytes", encoded_length)
                kind, width = "STRING", end - index + len(delimiter)
            elif char == "@":
                directive = next(
                    (item for item in ("@prefix", "@base") if self.text.startswith(item, index)),
                    None,
                )
                if directive is not None:
                    kind, value, width = "DIRECTIVE", directive.lower(), len(directive)
                else:
                    match = _LANG.match(self.text, index)
                    if match is None:
                        self._syntax("invalid language tag or directive", start_line, start_column)
                    kind, value, width = "LANG", match.group()[1:], match.end() - index
            else:
                number = _NUMBER.match(self.text, index)
                if number is not None and _number_boundary(self.text, number.end()):
                    kind, value, width = "NUMBER", number.group(), number.end() - index
                else:
                    end = index
                    while end < self.length and self.text[end] not in _WORD_STOP:
                        if self.text[end] == "\\" and end + 1 < self.length:
                            end += 2
                            continue
                        if self.text[end] == "." and (
                            end + 1 == self.length or self.text[end + 1] in " \t\r\n;,[]()"
                        ):
                            break
                        end += 1
                    if end == index:
                        self._syntax("unexpected Turtle character", start_line, start_column)
                    kind, value, width = "WORD", self.text[index:end], end - index
            index, line, column = _advance(self.text, index, width, line, column)
            output.append(TurtleToken(kind, value, start, start_line, start_column))
        output.append(TurtleToken("EOF", "", index, line, column))
        return tuple(output)

    def _escape(self, index: int) -> tuple[str, int]:
        if index + 1 >= self.length:
            self._syntax("truncated string escape", 1, 1)
        simple = {
            "t": "\t",
            "b": "\b",
            "n": "\n",
            "r": "\r",
            "f": "\f",
            '"': '"',
            "'": "'",
            "\\": "\\",
        }
        next_char = self.text[index + 1]
        if next_char in simple:
            return simple[next_char], 2
        match = _UCHAR.match(self.text, index)
        if match is None:
            self._syntax("invalid Turtle escape", 1, 1)
        codepoint = int(match.group(1) or match.group(2), 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            self._syntax("invalid Unicode escape", 1, 1)
        return chr(codepoint), match.end() - index

    @staticmethod
    def _syntax(message: str, line: int, column: int) -> NoReturn:
        raise OntologySyntaxError(
            f"{message} at line {line}, column {column}", code="TURTLE_SYNTAX"
        )


class TurtleParser:
    __slots__ = (
        "base",
        "blank_counter",
        "context",
        "index",
        "prefixes",
        "tokens",
        "triples",
    )

    def __init__(
        self,
        text: str,
        limits: ParseLimits,
        document_iri: IRI | None,
        cancellation_token: CancellationToken | None,
    ) -> None:
        self.context = ParseContext(limits, cancellation_token)
        self.tokens = TurtleLexer(text, self.context).tokenize()
        self.index = 0
        self.base = None if document_iri is None else document_iri.value
        self.prefixes: dict[str, str] = {}
        self.triples: set[Triple] = set()
        self.blank_counter = 0

    def parse(self) -> tuple[RDFGraph, dict[str, str]]:
        while not self._at("EOF"):
            if self._directive():
                self._parse_directive()
                continue
            subject = self._subject()
            self._predicate_object_list(subject, terminators={"."})
            self._expect(".")
        self.context.limits.enforce("max_triples", len(self.triples))
        return RDFGraph(self.triples), self.prefixes

    def _parse_directive(self) -> None:
        token = self._take()
        value = token.value.lower()
        if token.kind == "WORD":
            value = value.lower()
        if value in {"@prefix", "prefix"}:
            prefix = self._expect("WORD")
            if not prefix.value.endswith(":"):
                self._syntax("prefix label must end in ':'", prefix)
            iri = self._expect("IRI")
            self.prefixes[prefix.value[:-1]] = self._resolve_iri(iri.value)
            self.context.limits.enforce("max_prefixes", len(self.prefixes))
        elif value in {"@base", "base"}:
            iri = self._expect("IRI")
            self.base = self._resolve_iri(iri.value)
        else:
            self._syntax("unknown Turtle directive", token)
        if value.startswith("@"):
            self._expect(".")

    def _directive(self) -> bool:
        token = self._peek()
        return token.kind == "DIRECTIVE" or (
            token.kind == "WORD" and token.value.lower() in {"prefix", "base"}
        )

    def _predicate_object_list(self, subject: RDFResource, *, terminators: set[str]) -> None:
        while True:
            predicate = self._verb()
            while True:
                object_value = self._object()
                self._add(subject, predicate, object_value)
                if not self._at(","):
                    break
                self._take()
            if not self._at(";"):
                return
            while self._at(";"):
                self._take()
            if self._peek().kind in terminators or self._at("]"):
                return

    def _verb(self) -> RDFIRI:
        if self._peek().kind == "WORD" and self._peek().value == "a":
            self._take()
            return RDFIRI(RDF + "type")
        value = self._iri()
        return RDFIRI(value)

    def _subject(self) -> RDFResource:
        token = self._peek()
        if token.kind in {"IRI", "WORD"}:
            if token.kind == "WORD" and token.value.startswith("_:"):
                self._take()
                return RDFBlank(token.value[2:])
            return RDFIRI(self._iri())
        if token.kind == "[":
            return self._blank_property_list()
        if token.kind == "(":
            value = self._collection()
            if not isinstance(value, (RDFIRI, RDFBlank)):
                self._syntax("collection subject cannot be literal", token)
            return value
        self._syntax("expected Turtle subject", token)

    def _object(self) -> RDFTerm:
        token = self._peek()
        if token.kind == "STRING":
            self._take()
            if self._at("LANG"):
                return RDFLiteral(token.value, language=self._take().value)
            if self._at("HAT"):
                self._take()
                return RDFLiteral(token.value, self._iri())
            return RDFLiteral(token.value, XSD + "string")
        if token.kind == "NUMBER":
            self._take()
            datatype = (
                XSD + "double"
                if "e" in token.value.lower()
                else XSD + "decimal"
                if "." in token.value
                else XSD + "integer"
            )
            return RDFLiteral(token.value, datatype)
        if token.kind == "WORD" and token.value in {"true", "false"}:
            self._take()
            return RDFLiteral(token.value, XSD + "boolean")
        if token.kind == "WORD" and token.value.startswith("_:"):
            self._take()
            return RDFBlank(token.value[2:])
        if token.kind in {"IRI", "WORD"}:
            return RDFIRI(self._iri())
        if token.kind == "[":
            return self._blank_property_list()
        if token.kind == "(":
            return self._collection()
        self._syntax("expected Turtle object", token)

    def _blank_property_list(self) -> RDFBlank:
        self._expect("[")
        node = self._fresh("anon")
        if not self._at("]"):
            self._predicate_object_list(node, terminators={"]"})
        self._expect("]")
        return node

    def _collection(self) -> RDFResource:
        self._expect("(")
        values: list[RDFTerm] = []
        while not self._at(")"):
            values.append(self._object())
            self.context.limits.enforce("max_rdf_list_length", len(values))
        self._expect(")")
        if not values:
            return RDFIRI(RDF + "nil")
        nodes = [self._fresh("list") for _ in values]
        for index, (node, value) in enumerate(zip(nodes, values, strict=True)):
            self._add(node, RDFIRI(RDF + "first"), value)
            self._add(
                node,
                RDFIRI(RDF + "rest"),
                nodes[index + 1] if index + 1 < len(nodes) else RDFIRI(RDF + "nil"),
            )
        return nodes[0]

    def _iri(self) -> str:
        token = self._take()
        if token.kind == "IRI":
            return self._resolve_iri(_decode_uchar(token.value, self._syntax_message))
        if token.kind != "WORD" or ":" not in token.value or token.value.startswith("_:"):
            self._syntax("expected an IRI or prefixed name", token)
        prefix, local = token.value.split(":", 1)
        try:
            base = self.prefixes[prefix]
        except KeyError:
            self._syntax(f"undefined prefix {prefix!r}", token)
        return base + _decode_pname(local, self._syntax_message)

    def _resolve_iri(self, value: str) -> str:
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            result = value
        elif self.base is not None:
            result = urljoin(self.base, value)
        else:
            raise OntologySyntaxError(
                "relative IRI requires a document/base IRI", code="TURTLE_RELATIVE_IRI"
            )
        self.context.limits.enforce("max_iri_bytes", len(result.encode("utf-8")))
        return result

    def _add(self, subject: RDFResource, predicate: RDFIRI, object: RDFTerm) -> None:
        self.triples.add(Triple(subject, predicate, object))
        self.context.limits.enforce("max_triples", len(self.triples))

    def _fresh(self, stem: str) -> RDFBlank:
        self.blank_counter += 1
        return RDFBlank(f"{stem}-{self.blank_counter}")

    def _at(self, kind: str) -> bool:
        return self._peek().kind == kind

    def _peek(self) -> TurtleToken:
        return self.tokens[self.index]

    def _take(self) -> TurtleToken:
        value = self.tokens[self.index]
        self.index += 1
        self.context.check()
        return value

    def _expect(self, kind: str) -> TurtleToken:
        token = self._peek()
        if token.kind != kind:
            self._syntax(f"expected {kind!r}, found {token.value or token.kind!r}", token)
        return self._take()

    @staticmethod
    def _syntax(message: str, token: TurtleToken) -> NoReturn:
        raise OntologySyntaxError(
            f"{message} at line {token.line}, column {token.column}", code="TURTLE_SYNTAX"
        )

    @staticmethod
    def _syntax_message(message: str) -> NoReturn:
        raise OntologySyntaxError(message, code="TURTLE_SYNTAX")


def render_turtle(document: OntologyDocument) -> bytes:
    graph = RDFEncoder().encode(document)
    lines = [
        f"{_render_term(item.subject)} {_render_term(item.predicate)} {_render_term(item.object)} ."
        for item in graph.triples
    ]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _render_term(value: RDFTerm) -> str:
    if isinstance(value, RDFIRI):
        return "<" + _escape_iri(value.value) + ">"
    if isinstance(value, RDFBlank):
        return "_:" + re.sub(r"[^A-Za-z0-9_.-]", "_", value.label)
    lexical = (
        value.lexical.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    result = f'"{lexical}"'
    if value.language is not None:
        return result + "@" + value.language
    if value.datatype is not None:
        return result + "^^<" + _escape_iri(value.datatype) + ">"
    return result


def _escape_iri(value: str) -> str:
    output: list[str] = []
    for character in value:
        if character in '<>"{}|^`\\' or ord(character) <= 0x20:
            output.append(f"\\u{ord(character):04X}")
        else:
            output.append(character)
    return "".join(output)


def _advance(text: str, index: int, width: int, line: int, column: int) -> tuple[int, int, int]:
    fragment = text[index : index + width]
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
    return index + width, line, column


def _number_boundary(text: str, end: int) -> bool:
    return end == len(text) or text[end] in " \t\r\n;,.[]()"


def _decode_uchar(value: str, fail: Callable[[str], NoReturn]) -> str:
    def replace(match: re.Match[str]) -> str:
        return _decode_uchar_match(match, fail)

    result = _UCHAR.sub(replace, value)
    if "\\" in result:
        fail("invalid IRI escape")
    return result


def _decode_uchar_match(
    match: re.Match[str],
    fail: Callable[[str], NoReturn],
) -> str:
    codepoint = int(match.group(1) or match.group(2), 16)
    if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
        fail("invalid Unicode escape")
    return chr(codepoint)


def _decode_pname(value: str, fail: Callable[[str], NoReturn]) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "%":
            end = index + 3
            if end > len(value) or not all(
                character in "0123456789abcdefABCDEF" for character in value[index + 1 : end]
            ):
                fail("invalid prefixed-name percent escape")
            output.append(value[index:end])
            index = end
            continue
        if value[index] != "\\":
            output.append(value[index])
            index += 1
            continue
        uchar = _UCHAR.match(value, index)
        if uchar is not None:
            output.append(_decode_uchar_match(uchar, fail))
            index = uchar.end()
            continue
        if index + 1 == len(value):
            fail("truncated prefixed-name escape")
        escaped = value[index + 1]
        if escaped not in _PN_LOCAL_ESCAPES:
            fail("invalid prefixed-name escape")
        output.append(escaped)
        index += 2
    return "".join(output)


__all__ = ["TurtleLexer", "TurtleParser", "parse_turtle", "render_turtle"]
