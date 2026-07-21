"""Secure RDF/XML graph parser and deterministic OWL RDF/XML writer."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn, cast
from xml.parsers import expat

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import OntologyDocument
from pyowl_core.exceptions import InvalidIRIError, OntologySyntaxError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI

from .common import ParseContext, ParsedOntology
from .rdf import (
    OWL,
    RDF,
    RDFIRI,
    RDFS,
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

XML_NS = "http://www.w3.org/XML/1998/namespace"
_FORBIDDEN_XML_TEXT = re.compile(r"(?is)<!\s*(?:DOCTYPE|ENTITY)")
_XML_SPACE = frozenset(" \t\r\n")
_IRI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_XML_UNDEFINED_ENTITY = expat.errors.codes[expat.errors.XML_ERROR_UNDEFINED_ENTITY]


@dataclass(frozen=True, slots=True)
class _IRIReference:
    scheme: str | None
    authority: str | None
    path: str
    query: str | None
    fragment: str | None


def parse_rdfxml(
    data: bytes,
    *,
    limits: ParseLimits,
    document_iri: IRI | None,
    cancellation_token: CancellationToken | None = None,
    allow_partial_rdf_mapping: bool = False,
) -> ParsedOntology:
    text, source_encoding = _decode_xml_source(data)
    _validate_xml_envelope(text, source_encoding)
    if _FORBIDDEN_XML_TEXT.search(text):
        raise OntologySyntaxError(
            "DTD and entity declarations are forbidden",
            code="XML_FORBIDDEN_CONSTRUCT",
        )
    context = ParseContext(limits, cancellation_token)
    pull: ET.XMLPullParser[ET.Element[str]] = ET.XMLPullParser(events=("start", "end"))
    depth = 0
    root: ET.Element | None = None
    try:
        for offset in range(0, len(text), 64 * 1024):
            context.check()
            pull.feed(text[offset : offset + 64 * 1024])
            for raw_event in pull.read_events():
                event, element = cast(tuple[str, ET.Element], raw_event)
                if event == "start":
                    depth += 1
                    context.depth(depth)
                    root = element if root is None else root
                    if _namespace(element.tag) == "http://www.w3.org/2001/XInclude":
                        raise OntologySyntaxError(
                            "XInclude is forbidden", code="XML_FORBIDDEN_CONSTRUCT"
                        )
                else:
                    depth -= 1
        pull.close()
    except ET.ParseError as error:
        if error.code == _XML_UNDEFINED_ENTITY:
            raise OntologySyntaxError(
                "external or undefined XML entities are forbidden",
                code="XML_FORBIDDEN_CONSTRUCT",
            ) from error
        raise OntologySyntaxError("malformed RDF/XML document", code="RDFXML_SYNTAX") from error
    if root is None:
        raise OntologySyntaxError("empty RDF/XML document", code="RDFXML_ROOT")
    graph = RDFXMLGraphParser(root, limits, document_iri, cancellation_token).parse()
    mapped = RDFMapper(
        graph,
        limits=limits,
        document_iri=document_iri,
        cancellation_token=cancellation_token,
    ).map(allow_partial=allow_partial_rdf_mapping)
    return ParsedOntology(
        mapped.ontology_id,
        mapped.imports,
        mapped.annotations,
        mapped.axioms,
        mapped.extensions,
        occurrences=mapped.occurrences,
        rdf_mapping_report=mapped.rdf_mapping_report,
        decoded_codepoint_length=len(text),
    )


class RDFXMLGraphParser:
    __slots__ = (
        "base",
        "blank_counter",
        "context",
        "node_ids",
        "rdf_ids",
        "root",
        "triples",
    )

    def __init__(
        self,
        root: ET.Element,
        limits: ParseLimits,
        document_iri: IRI | None,
        cancellation_token: CancellationToken | None,
    ) -> None:
        self.root = root
        self.context = ParseContext(limits, cancellation_token)
        self.base = None if document_iri is None else document_iri.value
        self.blank_counter = 0
        self.node_ids: dict[str, RDFBlank] = {}
        self.rdf_ids: set[tuple[str, str]] = set()
        self.triples: set[Triple] = set()

    def parse(self) -> RDFGraph:
        base = self._base(self.root, self.base)
        language = self.root.get(f"{{{XML_NS}}}lang")
        if self.root.tag == _tag(RDF, "RDF"):
            if _has_non_whitespace_content(self.root):
                self._syntax("rdf:RDF cannot contain direct character data")
            for child in self.root:
                self._node(child, base, language)
        else:
            self._node(self.root, base, language)
        return RDFGraph(self.triples)

    def _node(
        self,
        element: ET.Element,
        parent_base: str | None,
        parent_language: str | None,
    ) -> RDFResource:
        self.context.check()
        if _has_non_whitespace_content(element):
            self._syntax("RDF node elements cannot contain direct character data")
        base = self._base(element, parent_base)
        language = element.get(f"{{{XML_NS}}}lang", parent_language)
        identities = [
            ("about", element.get(_tag(RDF, "about"))),
            ("ID", element.get(_tag(RDF, "ID"))),
            ("nodeID", element.get(_tag(RDF, "nodeID"))),
        ]
        present = [(name, value) for name, value in identities if value is not None]
        if len(present) > 1:
            self._syntax("RDF node element has multiple identity attributes")
        if not present:
            subject: RDFResource = self._fresh("node")
        elif present[0][0] == "nodeID":
            subject = self._node_id(present[0][1] or "")
        elif present[0][0] == "ID":
            subject = RDFIRI(self._rdf_id(present[0][1] or "", base))
        else:
            subject = RDFIRI(self._resolve(present[0][1] or "", base))
        if element.tag != _tag(RDF, "Description"):
            namespace = _namespace(element.tag)
            if not namespace:
                self._syntax("typed RDF node element requires a namespace")
            self._add(subject, RDF + "type", RDFIRI(_expanded(element.tag)))
        self._node_property_attributes(element, subject, base, language)
        li_index = 0
        for child in element:
            predicate = _expanded(child.tag)
            if predicate == RDF + "li":
                li_index += 1
                predicate = RDF + "_" + str(li_index)
            self._property(child, subject, predicate, base, language)
        return subject

    def _node_property_attributes(
        self,
        element: ET.Element,
        subject: RDFResource,
        base: str | None,
        language: str | None,
    ) -> None:
        ignored = {
            _tag(RDF, "about"),
            _tag(RDF, "ID"),
            _tag(RDF, "nodeID"),
            f"{{{XML_NS}}}base",
            f"{{{XML_NS}}}lang",
        }
        for name, value in element.attrib.items():
            if name in ignored:
                continue
            predicate = _expanded(name)
            if predicate == RDF + "type":
                self._add(subject, predicate, RDFIRI(self._resolve(value, base)))
            else:
                self._add(
                    subject,
                    predicate,
                    RDFLiteral(value, language=language)
                    if language
                    else RDFLiteral(value, XSD + "string"),
                )

    def _property(
        self,
        element: ET.Element,
        subject: RDFResource,
        predicate: str,
        parent_base: str | None,
        parent_language: str | None,
    ) -> None:
        self.context.check()
        base = self._base(element, parent_base)
        language = element.get(f"{{{XML_NS}}}lang", parent_language)
        resource = element.get(_tag(RDF, "resource"))
        node_id = element.get(_tag(RDF, "nodeID"))
        parse_type = element.get(_tag(RDF, "parseType"))
        datatype = element.get(_tag(RDF, "datatype"))
        statement_id = element.get(_tag(RDF, "ID"))
        reified = None if statement_id is None else RDFIRI(self._rdf_id(statement_id, base))
        modes = sum(item is not None for item in (resource, node_id, parse_type, datatype))
        if modes > 1:
            self._syntax("RDF property element has conflicting object attributes")
        triple: Triple
        if parse_type is not None:
            if parse_type == "Resource":
                if _has_non_whitespace_content(element):
                    self._syntax("parseType Resource cannot contain character data")
                resource_object = self._fresh("resource")
                triple = self._add(subject, predicate, resource_object)
                li_index = 0
                for child in element:
                    child_predicate = _expanded(child.tag)
                    if child_predicate == RDF + "li":
                        li_index += 1
                        child_predicate = RDF + "_" + str(li_index)
                    self._property(child, resource_object, child_predicate, base, language)
            elif parse_type == "Collection":
                if _has_non_whitespace_content(element):
                    self._syntax("parseType Collection cannot contain character data")
                members = [self._node(child, base, language) for child in element]
                triple = self._add(subject, predicate, self._collection(members))
            else:
                # RDF/XML 1.1 parseTypeOther is defined to behave exactly like
                # parseType="Literal" without emitting value-specific triples.
                lexical = (element.text or "") + "".join(
                    ET.tostring(child, encoding="unicode") for child in element
                )
                triple = self._add(subject, predicate, RDFLiteral(lexical, RDF + "XMLLiteral"))
        elif resource is not None or node_id is not None:
            if len(element) or _has_non_whitespace_content(element):
                self._syntax("resource-valued RDF property cannot contain content")
            attributed_object: RDFResource = (
                RDFIRI(self._resolve(resource or "", base))
                if resource is not None
                else self._node_id(node_id or "")
            )
            triple = self._add(subject, predicate, attributed_object)
            self._property_attributes(element, attributed_object, base, language)
        elif len(element):
            if len(element) != 1:
                self._syntax("RDF property element must contain exactly one node element")
            if _has_non_whitespace_content(element):
                self._syntax("resource property element cannot contain character data")
            if self._non_syntax_attributes(element) and datatype is None:
                self._syntax("empty property attributes cannot accompany a child node")
            child_object = self._node(element[0], base, language)
            triple = self._add(subject, predicate, child_object)
        else:
            property_attributes = self._non_syntax_attributes(element)
            if property_attributes and (element.text or "").strip() and datatype is None:
                self._syntax("empty property attributes cannot accompany literal content")
            if property_attributes and not (element.text or "").strip() and datatype is None:
                empty_object = self._fresh("empty")
                triple = self._add(subject, predicate, empty_object)
                self._property_attributes(element, empty_object, base, language)
            else:
                lexical = element.text or ""
                self.context.limits.enforce("max_literal_bytes", len(lexical.encode("utf-8")))
                if datatype is not None:
                    literal = RDFLiteral(lexical, self._resolve(datatype, base))
                elif language:
                    literal = RDFLiteral(lexical, language=language)
                else:
                    literal = RDFLiteral(lexical, XSD + "string")
                triple = self._add(subject, predicate, literal)
        if reified is not None:
            self._add(reified, RDF + "type", RDFIRI(RDF + "Statement"))
            self._add(reified, RDF + "subject", triple.subject)
            self._add(reified, RDF + "predicate", triple.predicate)
            self._add(reified, RDF + "object", triple.object)

    def _property_attributes(
        self,
        element: ET.Element,
        subject: RDFResource,
        base: str | None,
        language: str | None,
    ) -> None:
        for name, value in self._non_syntax_attributes(element).items():
            predicate = _expanded(name)
            if predicate == RDF + "type":
                self._add(subject, predicate, RDFIRI(self._resolve(value, base)))
            else:
                literal = (
                    RDFLiteral(value, language=language)
                    if language
                    else RDFLiteral(value, XSD + "string")
                )
                self._add(subject, predicate, literal)

    @staticmethod
    def _non_syntax_attributes(element: ET.Element) -> dict[str, str]:
        syntax = {
            _tag(RDF, "resource"),
            _tag(RDF, "nodeID"),
            _tag(RDF, "parseType"),
            _tag(RDF, "datatype"),
            _tag(RDF, "ID"),
            f"{{{XML_NS}}}base",
            f"{{{XML_NS}}}lang",
        }
        return {key: value for key, value in element.attrib.items() if key not in syntax}

    def _collection(self, values: Sequence[RDFTerm]) -> RDFResource:
        if not values:
            return RDFIRI(RDF + "nil")
        self.context.limits.enforce("max_rdf_list_length", len(values))
        nodes = [self._fresh("list") for _ in values]
        for index, (node, value) in enumerate(zip(nodes, values, strict=True)):
            self._add(node, RDF + "first", value)
            self._add(
                node,
                RDF + "rest",
                nodes[index + 1] if index + 1 < len(nodes) else RDFIRI(RDF + "nil"),
            )
        return nodes[0]

    def _base(self, element: ET.Element, parent: str | None) -> str | None:
        local = element.get(f"{{{XML_NS}}}base")
        if local is None:
            return parent
        return self._resolve(local, parent)

    def _resolve(self, value: str, base: str | None) -> str:
        self._enforce_iri_size(value)
        reference = _parse_iri_reference(value)
        if reference.scheme is not None:
            scheme = reference.scheme
            authority = reference.authority
            path = _remove_dot_segments(reference.path)
            query = reference.query
        elif base is not None:
            try:
                parsed_base = _parse_iri_reference(base)
            except OntologySyntaxError as error:
                raise OntologySyntaxError(
                    "RDF/XML base IRI is not an absolute RFC 3986 IRI",
                    code="RDFXML_INVALID_BASE_IRI",
                ) from error
            if parsed_base.scheme is None:
                raise OntologySyntaxError(
                    "RDF/XML base IRI is not an absolute RFC 3986 IRI",
                    code="RDFXML_INVALID_BASE_IRI",
                )
            scheme = parsed_base.scheme
            if reference.authority is not None:
                authority = reference.authority
                path = _remove_dot_segments(reference.path)
                query = reference.query
            elif not reference.path:
                authority = parsed_base.authority
                path = parsed_base.path
                query = reference.query if reference.query is not None else parsed_base.query
            elif reference.path.startswith("/"):
                authority = parsed_base.authority
                path = _remove_dot_segments(reference.path)
                query = reference.query
            else:
                authority = parsed_base.authority
                prefix = (
                    "/"
                    if parsed_base.authority is not None and not parsed_base.path
                    else parsed_base.path[: parsed_base.path.rfind("/") + 1]
                )
                merged = prefix + reference.path
                self._enforce_iri_size(merged)
                path = _remove_dot_segments(merged)
                query = reference.query
        else:
            raise OntologySyntaxError(
                "relative RDF/XML IRI requires an absolute base",
                code="RDFXML_RELATIVE_IRI_NO_BASE",
            )
        result = _serialize_iri(
            scheme,
            authority,
            path,
            query,
            reference.fragment,
        )
        self._enforce_iri_size(result)
        try:
            IRI(result)
        except InvalidIRIError as error:
            raise OntologySyntaxError(
                "RDF/XML contains a relative or invalid IRI",
                code="RDFXML_SYNTAX",
            ) from error
        return result

    def _enforce_iri_size(self, value: str) -> None:
        self.context.limits.enforce("max_iri_bytes", len(value.encode("utf-8")))

    def _node_id(self, value: str) -> RDFBlank:
        if not _is_xml_ncname(value):
            self._syntax("rdf:nodeID must be an XML NCName")
        return self.node_ids.setdefault(value, RDFBlank(value))

    def _rdf_id(self, value: str, base: str | None) -> str:
        if not _is_xml_ncname(value):
            self._syntax("rdf:ID must be an XML NCName")
        resolved = self._resolve("#" + value, base)
        if base is None:
            raise AssertionError("resolved rdf:ID has no base")
        binding = (value, base)
        if binding in self.rdf_ids:
            self._syntax("rdf:ID must be unique within its XML base")
        self.rdf_ids.add(binding)
        return resolved

    def _fresh(self, stem: str) -> RDFBlank:
        self.blank_counter += 1
        return RDFBlank(f"{stem}-{self.blank_counter}")

    def _add(self, subject: RDFResource, predicate: str, object: RDFTerm) -> Triple:
        triple = Triple(subject, RDFIRI(predicate), object)
        self.triples.add(triple)
        self.context.limits.enforce("max_triples", len(self.triples))
        return triple

    @staticmethod
    def _syntax(message: str) -> NoReturn:
        raise OntologySyntaxError(message, code="RDFXML_SYNTAX")


def render_rdfxml(document: OntologyDocument) -> bytes:
    graph = RDFEncoder().encode(document)
    ET.register_namespace("rdf", RDF)
    ET.register_namespace("owl", OWL)
    ET.register_namespace("rdfs", RDFS)
    ET.register_namespace("xsd", XSD)
    namespaces = _predicate_namespaces(graph)
    for index, namespace in enumerate(
        item for item in sorted(namespaces) if item not in {RDF, OWL, RDFS, XSD}
    ):
        ET.register_namespace(f"p{index}", namespace)
    root = ET.Element(_tag(RDF, "RDF"))
    grouped: dict[RDFResource, list[Triple]] = defaultdict(list)
    for triple in graph.triples:
        grouped[triple.subject].append(triple)
    for subject in sorted(grouped, key=_resource_sort_key):
        attributes = (
            {_tag(RDF, "about"): subject.value}
            if isinstance(subject, RDFIRI)
            else {_tag(RDF, "nodeID"): _safe_node_id(subject.label)}
        )
        description = ET.SubElement(root, _tag(RDF, "Description"), attributes)
        for triple in sorted(grouped[subject], key=Triple.key):
            namespace, local = _split_predicate(triple.predicate.value)
            property_element = ET.SubElement(description, _tag(namespace, local))
            object_value = triple.object
            if isinstance(object_value, RDFIRI):
                property_element.set(_tag(RDF, "resource"), object_value.value)
            elif isinstance(object_value, RDFBlank):
                property_element.set(_tag(RDF, "nodeID"), _safe_node_id(object_value.label))
            else:
                property_element.text = object_value.lexical
                if object_value.language is not None:
                    property_element.set(f"{{{XML_NS}}}lang", object_value.language)
                elif object_value.datatype is not None:
                    property_element.set(_tag(RDF, "datatype"), object_value.datatype)
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


def _predicate_namespaces(graph: RDFGraph) -> set[str]:
    return {_split_predicate(item.predicate.value)[0] for item in graph.triples}


def _split_predicate(value: str) -> tuple[str, str]:
    # Choose the longest valid QName local part. Iterating from the beginning
    # avoids pathological one-character locals such as splitting ``rdf:type``
    # into a namespace ending in ``typ`` and the local name ``e``.
    for index in range(1, len(value)):
        local = value[index:]
        if _is_xml_ncname(local):
            return value[:index], local
    raise OntologySyntaxError(
        "predicate IRI cannot be represented as an RDF/XML QName",
        code="RDFXML_PREDICATE_QNAME",
    )


def _is_xml_ncname(value: str) -> bool:
    if not value or not _is_xml_name_start(value[0]):
        return False
    return all(character != ":" and _is_xml_name_character(character) for character in value[1:])


def _is_xml_name_start(value: str) -> bool:
    codepoint = ord(value)
    return (
        value == "_"
        or "A" <= value <= "Z"
        or "a" <= value <= "z"
        or 0x00C0 <= codepoint <= 0x00D6
        or 0x00D8 <= codepoint <= 0x00F6
        or 0x00F8 <= codepoint <= 0x02FF
        or 0x0370 <= codepoint <= 0x037D
        or 0x037F <= codepoint <= 0x1FFF
        or 0x200C <= codepoint <= 0x200D
        or 0x2070 <= codepoint <= 0x218F
        or 0x2C00 <= codepoint <= 0x2FEF
        or 0x3001 <= codepoint <= 0xD7FF
        or 0xF900 <= codepoint <= 0xFDCF
        or 0xFDF0 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0xEFFFF
    )


def _is_xml_name_character(value: str) -> bool:
    codepoint = ord(value)
    return (
        _is_xml_name_start(value)
        or value in {"-", "."}
        or "0" <= value <= "9"
        or codepoint == 0x00B7
        or 0x0300 <= codepoint <= 0x036F
        or 0x203F <= codepoint <= 0x2040
    )


def _safe_node_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return cleaned if cleaned and (cleaned[0].isalpha() or cleaned[0] == "_") else "b" + cleaned


def _has_non_whitespace_content(element: ET.Element) -> bool:
    return bool((element.text or "").strip()) or any(
        bool((child.tail or "").strip()) for child in element
    )


def _resource_sort_key(value: RDFResource) -> tuple[str, str]:
    return ("I", value.value) if isinstance(value, RDFIRI) else ("B", value.label)


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _expanded(tag: str) -> str:
    if not tag.startswith("{") or "}" not in tag:
        raise OntologySyntaxError(
            "RDF/XML element and property attributes require namespaces",
            code="RDFXML_NAMESPACE",
        )
    namespace, local = tag[1:].split("}", 1)
    return namespace + local


def _parse_iri_reference(value: str) -> _IRIReference:
    without_fragment, separator, fragment = value.partition("#")
    selected_fragment = fragment if separator else None
    hierarchical, separator, query = without_fragment.partition("?")
    selected_query = query if separator else None
    first_slash = hierarchical.find("/")
    first_slash = len(hierarchical) if first_slash < 0 else first_slash
    colon = hierarchical.find(":")
    if 0 <= colon < first_slash:
        scheme = hierarchical[:colon]
        if _IRI_SCHEME.fullmatch(scheme) is None:
            raise OntologySyntaxError(
                "RDF/XML IRI reference has an invalid scheme",
                code="RDFXML_IRI_REFERENCE",
            )
        remainder = hierarchical[colon + 1 :]
    else:
        scheme = None
        remainder = hierarchical
    if remainder.startswith("//"):
        authority_and_path = remainder[2:]
        slash = authority_and_path.find("/")
        if slash < 0:
            authority, path = authority_and_path, ""
        else:
            authority = authority_and_path[:slash]
            path = authority_and_path[slash:]
    else:
        authority, path = None, remainder
    return _IRIReference(scheme, authority, path, selected_query, selected_fragment)


def _remove_dot_segments(path: str) -> str:
    output = ""
    remaining = path
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith(("./", "/./")):
            remaining = remaining[2:]
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = remaining[3:]
            output = output[: max(output.rfind("/"), 0)]
        elif remaining == "/..":
            remaining = "/"
            output = output[: max(output.rfind("/"), 0)]
        elif remaining in {".", ".."}:
            remaining = ""
        else:
            if remaining.startswith("/"):
                next_slash = remaining.find("/", 1)
            else:
                next_slash = remaining.find("/")
            end = len(remaining) if next_slash < 0 else next_slash
            output += remaining[:end]
            remaining = remaining[end:]
    return output


def _serialize_iri(
    scheme: str,
    authority: str | None,
    path: str,
    query: str | None,
    fragment: str | None,
) -> str:
    result = scheme + ":"
    if authority is not None:
        result += "//" + authority
    result += path
    if query is not None:
        result += "?" + query
    if fragment is not None:
        result += "#" + fragment
    return result


def _decode_xml_source(data: bytes) -> tuple[str, str]:
    if data.startswith(
        (
            b"\xff\xfe\x00\x00",
            b"\x00\x00\xfe\xff",
            b"\x00\x00\x00<",
            b"<\x00\x00\x00",
        )
    ):
        raise OntologySyntaxError(
            "RDF/XML source uses unsupported UTF-32 encoding",
            code="FORMAT_ENCODING",
        )
    if data.startswith(b"\xff\xfe"):
        content, codec, source_encoding = data[2:], "utf-16-le", "utf-16-le"
    elif data.startswith(b"\xfe\xff"):
        content, codec, source_encoding = data[2:], "utf-16-be", "utf-16-be"
    elif data.startswith(b"<\x00"):
        content, codec, source_encoding = data, "utf-16-le", "utf-16-le"
    elif data.startswith(b"\x00<"):
        content, codec, source_encoding = data, "utf-16-be", "utf-16-be"
    elif data.startswith(b"\xef\xbb\xbf"):
        content, codec, source_encoding = data[3:], "utf-8", "utf-8"
    else:
        content, codec, source_encoding = data, "utf-8", "utf-8"
    try:
        return content.decode(codec), source_encoding
    except UnicodeDecodeError as error:
        raise OntologySyntaxError(
            "invalid or unsupported XML encoding",
            code="FORMAT_ENCODING",
        ) from error


def _validate_xml_envelope(text: str, source_encoding: str) -> None:
    if not text.startswith("<?"):
        return
    declaration_end = text.find("?>", 2)
    if declaration_end < 0:
        return
    declaration = text[2:declaration_end]
    target_end = 0
    while target_end < len(declaration) and declaration[target_end] not in _XML_SPACE:
        target_end += 1
    target = declaration[:target_end]
    if not target.isascii() or target.lower() != "xml":
        return
    if target != "xml":
        _xml_declaration_syntax()
    _validate_xml_declaration(declaration, source_encoding)


def _validate_xml_declaration(declaration: str, source_encoding: str) -> None:
    if not declaration.startswith("xml") or (
        len(declaration) == 3 or declaration[3] not in _XML_SPACE
    ):
        _xml_declaration_syntax()
    cursor = _skip_xml_space(declaration, 3)
    name, version, cursor = _xml_declaration_attribute(declaration, cursor)
    if name != "version" or version != "1.0":
        _xml_declaration_syntax()
    encoding_seen = False
    standalone_seen = False
    while cursor < len(declaration):
        if declaration[cursor] not in _XML_SPACE:
            _xml_declaration_syntax()
        cursor = _skip_xml_space(declaration, cursor)
        if cursor == len(declaration):
            break
        name, value, cursor = _xml_declaration_attribute(declaration, cursor)
        if name == "encoding" and not encoding_seen and not standalone_seen:
            encoding_seen = True
            compatible = {
                "utf-8": frozenset(("utf-8", "utf8", "us-ascii")),
                "utf-16-le": frozenset(("utf-16", "utf-16le")),
                "utf-16-be": frozenset(("utf-16", "utf-16be")),
            }[source_encoding]
            if not value.isascii() or value.lower() not in compatible:
                raise OntologySyntaxError(
                    "XML declaration encoding does not match the source",
                    code="XML_FORBIDDEN_CONSTRUCT",
                )
        elif name == "standalone" and not standalone_seen:
            standalone_seen = True
            if value not in {"yes", "no"}:
                _xml_declaration_syntax()
        else:
            _xml_declaration_syntax()


def _xml_declaration_attribute(declaration: str, cursor: int) -> tuple[str, str, int]:
    name_start = cursor
    while cursor < len(declaration):
        character = declaration[cursor]
        if not character.isascii() or not character.isalpha():
            break
        cursor += 1
    if cursor == name_start:
        _xml_declaration_syntax()
    name = declaration[name_start:cursor]
    cursor = _skip_xml_space(declaration, cursor)
    if cursor == len(declaration) or declaration[cursor] != "=":
        _xml_declaration_syntax()
    cursor = _skip_xml_space(declaration, cursor + 1)
    if cursor == len(declaration) or declaration[cursor] not in {'"', "'"}:
        _xml_declaration_syntax()
    quote = declaration[cursor]
    cursor += 1
    value_start = cursor
    while cursor < len(declaration) and declaration[cursor] != quote:
        if declaration[cursor] in "<&":
            _xml_declaration_syntax()
        cursor += 1
    if cursor == len(declaration):
        _xml_declaration_syntax()
    return name, declaration[value_start:cursor], cursor + 1


def _skip_xml_space(value: str, cursor: int) -> int:
    while cursor < len(value) and value[cursor] in _XML_SPACE:
        cursor += 1
    return cursor


def _xml_declaration_syntax() -> NoReturn:
    raise OntologySyntaxError("malformed XML declaration", code="RDFXML_SYNTAX")


__all__ = ["RDFXMLGraphParser", "parse_rdfxml", "render_rdfxml"]
