"""Immutable one-document OWL structure and document-scoped identity."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Literal as TypingLiteral
from typing import TypeVar, cast

from pyowl_core.diagnostics import Diagnostic
from pyowl_core.model import (
    AXIOM_TYPES,
    IRI,
    Annotation,
    AnonymousIndividual,
    BlankNodeArc,
    CanonicalSet,
    Entity,
    EntityKind,
    Literal,
    StructuralNode,
    alpha_canonicalize_blank_nodes,
    canonical_bytes,
    canonical_document_scope,
    constructor_spec,
    encode_varint,
)
from pyowl_core.model import (
    signature as node_signature,
)
from pyowl_core.model.axioms import AxiomNode

from .provenance import DocumentProvenance, OriginIndex, RDFMappingReport, SourceMap

A = TypeVar("A", bound=AxiomNode)
_DOCUMENT_DOMAIN = b"pyowl-core:document-fingerprint:v1\x00"
_LEXICAL_KEY = b"pyowl-core:parser-blank-label:v1\x00"
_PROVISIONAL_SCOPE = hashlib.sha256(b"pyowl-core:provisional-document-scope:v1\x00").digest()


@dataclass(frozen=True, slots=True, order=True)
class OntologyID:
    ontology_iri: IRI | None = None
    version_iri: IRI | None = None

    def __post_init__(self) -> None:
        if self.ontology_iri is not None and not isinstance(self.ontology_iri, IRI):
            raise TypeError("ontology_iri must be IRI or None")
        if self.version_iri is not None and not isinstance(self.version_iri, IRI):
            raise TypeError("version_iri must be IRI or None")
        if self.version_iri is not None and self.ontology_iri is None:
            raise ValueError("version_iri requires ontology_iri")


@dataclass(frozen=True, slots=True, order=True)
class Fingerprint:
    algorithm: TypingLiteral["sha256"]
    schema: int
    digest: bytes

    def __post_init__(self) -> None:
        if self.algorithm != "sha256":
            raise ValueError("algorithm must be 'sha256'")
        if isinstance(self.schema, bool) or not isinstance(self.schema, int) or self.schema < 1:
            raise ValueError("schema must be a positive integer")
        if not isinstance(self.digest, bytes) or len(self.digest) != 32:
            raise ValueError("digest must be exactly 32 bytes")

    @property
    def hex(self) -> str:
        return self.digest.hex()


@dataclass(frozen=True, slots=True, eq=False)
class OntologyDocument:
    ontology_id: OntologyID
    document_iri: IRI | None
    direct_imports: tuple[IRI, ...]
    ontology_annotations: CanonicalSet[Annotation]
    axioms: CanonicalSet[AxiomNode]
    extension_components: CanonicalSet[StructuralNode]
    provenance: DocumentProvenance
    source_map: SourceMap | None = None
    origin_index: OriginIndex | None = None
    rdf_mapping_report: RDFMappingReport | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ontology_id, OntologyID):
            raise TypeError("ontology_id must be OntologyID")
        if self.document_iri is not None and not isinstance(self.document_iri, IRI):
            raise TypeError("document_iri must be IRI or None")
        imports = tuple(sorted(set(self.direct_imports), key=canonical_bytes))
        if not all(isinstance(item, IRI) for item in imports):
            raise TypeError("direct_imports must contain IRI values")
        annotations = (
            self.ontology_annotations
            if isinstance(self.ontology_annotations, CanonicalSet)
            else CanonicalSet(self.ontology_annotations)
        )
        if not all(isinstance(item, Annotation) for item in annotations):
            raise TypeError("ontology_annotations must contain Annotation values")
        axioms = self.axioms if isinstance(self.axioms, CanonicalSet) else CanonicalSet(self.axioms)
        if not all(isinstance(item, AXIOM_TYPES) for item in axioms):
            raise TypeError("axioms must contain OWL axioms")
        extensions = (
            self.extension_components
            if isinstance(self.extension_components, CanonicalSet)
            else CanonicalSet(self.extension_components)
        )
        if not all(isinstance(item, StructuralNode) for item in extensions):
            raise TypeError("extension_components must contain structural values")
        if not isinstance(self.provenance, DocumentProvenance):
            raise TypeError("provenance must be DocumentProvenance")
        if self.source_map is not None and not isinstance(self.source_map, SourceMap):
            raise TypeError("source_map must be SourceMap or None")
        if self.origin_index is not None and not isinstance(self.origin_index, OriginIndex):
            raise TypeError("origin_index must be OriginIndex or None")
        if self.rdf_mapping_report is not None and not isinstance(
            self.rdf_mapping_report, RDFMappingReport
        ):
            raise TypeError("rdf_mapping_report must be RDFMappingReport or None")
        object.__setattr__(self, "direct_imports", imports)
        object.__setattr__(self, "ontology_annotations", annotations)
        object.__setattr__(self, "axioms", axioms)
        object.__setattr__(self, "extension_components", extensions)
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(item, Diagnostic) for item in diagnostics):
            raise TypeError("diagnostics must contain Diagnostic values")
        object.__setattr__(self, "diagnostics", diagnostics)

    def iter_axioms(self, axiom_type: type[A] | None = None) -> Iterator[AxiomNode | A]:
        if axiom_type is None:
            yield from self.axioms
            return
        if not isinstance(axiom_type, type) or not issubclass(axiom_type, AxiomNode):
            raise TypeError("axiom_type must be an axiom class or None")
        yield from cast(Iterator[A], (item for item in self.axioms if type(item) is axiom_type))

    def iter_extensions(self, namespace: str | None = None) -> Iterator[StructuralNode]:
        if namespace not in {None, "swrl"}:
            return
        yield from self.extension_components

    def signature(
        self,
        kind: EntityKind | None = None,
        *,
        include_builtins: bool = True,
    ) -> tuple[Entity, ...]:
        if kind is not None and not isinstance(kind, EntityKind):
            raise TypeError("kind must be EntityKind or None")
        gathered: set[Entity] = set()
        for value in (*self.ontology_annotations, *self.axioms, *self.extension_components):
            gathered.update(node_signature(value))
        if not include_builtins:
            gathered = {item for item in gathered if not _is_builtin(item)}
        if kind is not None:
            gathered = {item for item in gathered if item.kind is kind}
        return tuple(sorted(gathered, key=canonical_bytes))

    @property
    def document_fingerprint(self) -> Fingerprint:
        return Fingerprint("sha256", 1, hashlib.sha256(_document_bytes(self)).digest())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OntologyDocument):
            return NotImplemented
        return _document_bytes(self) == _document_bytes(other)

    def __hash__(self) -> int:
        value = int.from_bytes(self.document_fingerprint.digest[:8], "big", signed=True)
        return -2 if value == -1 else value


def provisional_anonymous(label: str) -> AnonymousIndividual:
    if not isinstance(label, str) or not label:
        raise ValueError("blank label must be a nonempty string")
    encoded = label.encode("utf-8")
    return AnonymousIndividual(
        _PROVISIONAL_SCOPE, _LEXICAL_KEY + encode_varint(len(encoded)) + encoded
    )


def provisional_label(value: AnonymousIndividual) -> str | None:
    if value.document_scope != _PROVISIONAL_SCOPE or not value.local_key.startswith(_LEXICAL_KEY):
        return None
    payload = value.local_key[len(_LEXICAL_KEY) :]
    length, offset = _decode_varint(payload)
    raw = payload[offset : offset + length]
    if offset + length != len(payload):
        return None
    return raw.decode("utf-8")


def freeze_document_anonymous(
    ontology_id: OntologyID,
    direct_imports: Iterable[IRI],
    ontology_annotations: Iterable[Annotation],
    axioms: Iterable[AxiomNode],
    extension_components: Iterable[StructuralNode],
    *,
    limits: object | None = None,
) -> tuple[
    tuple[IRI, ...],
    CanonicalSet[Annotation],
    CanonicalSet[AxiomNode],
    CanonicalSet[StructuralNode],
]:
    imports = tuple(sorted(set(direct_imports), key=canonical_bytes))
    annotations = tuple(ontology_annotations)
    axiom_values = tuple(axioms)
    extensions = tuple(extension_components)
    roots: tuple[StructuralNode, ...] = (*annotations, *axiom_values, *extensions)
    labels = sorted(
        {
            label
            for root in roots
            for individual in _anonymous_values(root)
            if (label := provisional_label(individual)) is not None
        }
    )
    if not labels:
        return (
            imports,
            CanonicalSet(annotations),
            CanonicalSet(axiom_values),
            CanonicalSet(extensions),
        )
    arcs = _blank_arcs(roots)
    first = alpha_canonicalize_blank_nodes(arcs, _PROVISIONAL_SCOPE, labels=labels, limits=limits)
    ontology_key = _ontology_key(ontology_id)
    scope = canonical_document_scope(ontology_key, canonical_graph=first.canonical_graph)
    result = alpha_canonicalize_blank_nodes(arcs, scope, labels=labels, limits=limits)
    replacements = result.as_mapping()
    replaced_annotations = CanonicalSet(
        cast(Annotation, _replace_blanks(item, replacements)) for item in annotations
    )
    replaced_axioms = CanonicalSet(
        cast(AxiomNode, _replace_blanks(item, replacements)) for item in axiom_values
    )
    replaced_extensions = CanonicalSet(
        cast(StructuralNode, _replace_blanks(item, replacements)) for item in extensions
    )
    return imports, replaced_annotations, replaced_axioms, replaced_extensions


def _ontology_key(ontology_id: OntologyID) -> bytes:
    if ontology_id.ontology_iri is None:
        return b"anonymous-ontology"
    payload = canonical_bytes(ontology_id.ontology_iri)
    if ontology_id.version_iri is not None:
        payload += canonical_bytes(ontology_id.version_iri)
    return payload


def _blank_arcs(roots: tuple[StructuralNode, ...]) -> tuple[BlankNodeArc, ...]:
    arcs: list[BlankNodeArc] = []
    for root in roots:
        skeleton = _skeleton(root)
        occurrences = tuple(_blank_occurrences(root, (type(root).__name__,)))
        for label, path in occurrences:
            arcs.append(BlankNodeArc(label, "/".join(path), payload=skeleton))
        for index, (source, source_path) in enumerate(occurrences):
            for target, target_path in occurrences[index + 1 :]:
                arcs.append(
                    BlankNodeArc(
                        source,
                        "/".join(source_path) + "->" + "/".join(target_path),
                        target,
                        skeleton,
                    )
                )
    return tuple(arcs)


def _blank_occurrences(
    value: object, path: tuple[str, ...]
) -> Iterator[tuple[str, tuple[str, ...]]]:
    if isinstance(value, AnonymousIndividual):
        label = provisional_label(value)
        if label is not None:
            yield label, path
        return
    if isinstance(value, CanonicalSet):
        grouped = sorted(value, key=_skeleton)
        for item in grouped:
            marker = hashlib.sha256(_skeleton(item)).hexdigest()[:16]
            yield from _blank_occurrences(item, (*path, f"set:{marker}"))
        return
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            yield from _blank_occurrences(item, (*path, str(index)))
        return
    if isinstance(value, StructuralNode) and is_dataclass(value):
        for item in fields(value):
            yield from _blank_occurrences(getattr(value, item.name), (*path, item.name))


def _anonymous_values(value: object) -> Iterator[AnonymousIndividual]:
    if isinstance(value, AnonymousIndividual):
        yield value
    elif isinstance(value, (CanonicalSet, tuple)):
        for item in value:
            yield from _anonymous_values(item)
    elif isinstance(value, StructuralNode) and is_dataclass(value):
        for item in fields(value):
            yield from _anonymous_values(getattr(value, item.name))


def _skeleton(value: object) -> bytes:
    if isinstance(value, AnonymousIndividual):
        return b"B"
    if isinstance(value, CanonicalSet):
        set_members = sorted(_skeleton(item) for item in value)
        return (
            b"S"
            + encode_varint(len(set_members))
            + b"".join(encode_varint(len(item)) + item for item in set_members)
        )
    if isinstance(value, tuple):
        tuple_members = tuple(_skeleton(item) for item in value)
        return (
            b"Q"
            + encode_varint(len(tuple_members))
            + b"".join(encode_varint(len(item)) + item for item in tuple_members)
        )
    if isinstance(value, StructuralNode):
        if not any(True for _ in _anonymous_values(value)):
            encoded = canonical_bytes(value)
            return b"C" + encode_varint(len(encoded)) + encoded
        spec = constructor_spec(value)
        field_members = tuple(_skeleton(getattr(value, name)) for name in spec.fields)
        return (
            b"N"
            + encode_varint(spec.tag)
            + b"".join(encode_varint(len(item)) + item for item in field_members)
        )
    if value is None:
        return b"0"
    if isinstance(value, int):
        return b"I" + encode_varint(value)
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return b"T" + encode_varint(len(encoded)) + encoded
    raise TypeError(f"unsupported skeleton value {type(value).__name__}")


def _replace_blanks(value: object, replacements: Mapping[str, AnonymousIndividual]) -> object:
    if isinstance(value, AnonymousIndividual):
        label = provisional_label(value)
        return value if label is None else replacements[label]
    if isinstance(value, CanonicalSet):
        return CanonicalSet(
            cast(StructuralNode, _replace_blanks(item, replacements)) for item in value
        )
    if isinstance(value, tuple):
        return tuple(_replace_blanks(item, replacements) for item in value)
    if not isinstance(value, StructuralNode) or isinstance(value, (IRI, Entity, Literal)):
        return value
    if not is_dataclass(value):
        return value
    arguments = {
        item.name: _replace_blanks(getattr(value, item.name), replacements)
        for item in fields(value)
    }
    return type(value)(**arguments)


def _document_bytes(document: OntologyDocument) -> bytes:
    pieces: list[bytes] = [_DOCUMENT_DOMAIN]
    for iri in (document.ontology_id.ontology_iri, document.ontology_id.version_iri):
        if iri is None:
            pieces.append(b"0")
        else:
            encoded = canonical_bytes(iri)
            pieces.append(b"1" + encode_varint(len(encoded)) + encoded)
    for collection in (
        document.direct_imports,
        document.ontology_annotations,
        document.axioms,
        document.extension_components,
    ):
        members = tuple(canonical_bytes(item) for item in collection)
        pieces.append(encode_varint(len(members)))
        pieces.extend(encode_varint(len(item)) + item for item in members)
    return b"".join(pieces)


def _decode_varint(value: bytes) -> tuple[int, int]:
    result = 0
    shift = 0
    for index, octet in enumerate(value):
        result |= (octet & 0x7F) << shift
        if octet < 0x80:
            return result, index + 1
        shift += 7
    raise ValueError("truncated varint")


def _is_builtin(entity: Entity) -> bool:
    iri = entity.iri.value
    return iri.startswith(
        (
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "http://www.w3.org/2000/01/rdf-schema#",
            "http://www.w3.org/2001/XMLSchema#",
            "http://www.w3.org/2002/07/owl#",
        )
    )


__all__ = [
    "Fingerprint",
    "OntologyDocument",
    "OntologyID",
    "freeze_document_anonymous",
]
