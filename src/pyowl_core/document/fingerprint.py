"""Canonical, domain-separated document and ontology-view fingerprints."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, cast

from pyowl_core.model import (
    LOGICAL_AXIOM_TYPES,
    Annotation,
    CanonicalSet,
    Entity,
    StructuralNode,
    canonical_bytes,
    encode_varint,
)
from pyowl_core.model.axioms import AxiomNode

from .document import Fingerprint

if TYPE_CHECKING:
    from .document import OntologyDocument
    from .imports import ImportManifest


class _HashWriter(Protocol):
    def update(self, data: bytes, /) -> object: ...


class StructuralContextKind(str, Enum):
    """The retained document/member boundary semantics of a materialized view."""

    OVERLAY = "overlay"
    COMPOSITE = "composite"


@dataclass(frozen=True, slots=True)
class StructuralContext:
    """Small canonical boundary manifest retained across materialization."""

    kind: StructuralContextKind
    fingerprints: tuple[Fingerprint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StructuralContextKind):
            raise TypeError("kind must be StructuralContextKind")
        values = tuple(self.fingerprints)
        if not all(isinstance(item, Fingerprint) for item in values):
            raise TypeError("fingerprints must contain Fingerprint values")
        if self.kind is StructuralContextKind.OVERLAY and len(values) != 1:
            raise ValueError("overlay structural context requires exactly one anchor")
        if self.kind is StructuralContextKind.COMPOSITE and len(values) < 2:
            raise ValueError("composite structural context requires at least two members")
        if self.kind is StructuralContextKind.COMPOSITE:
            values = tuple(sorted(values, key=fingerprint_bytes))
        object.__setattr__(self, "fingerprints", values)

    @classmethod
    def overlay(cls, anchor: Fingerprint) -> StructuralContext:
        return cls(StructuralContextKind.OVERLAY, (anchor,))

    @classmethod
    def composite(cls, members: Iterable[Fingerprint]) -> StructuralContext:
        return cls(StructuralContextKind.COMPOSITE, tuple(members))

    def canonical_bytes(self) -> bytes:
        pieces = [
            b"pyowl-core:view-structure-context:v1\x00",
            _frame(self.kind.value.encode("ascii")),
            encode_varint(len(self.fingerprints)),
        ]
        pieces.extend(_frame(fingerprint_bytes(item)) for item in self.fingerprints)
        return b"".join(pieces)


def fingerprint_bytes(value: Fingerprint) -> bytes:
    if not isinstance(value, Fingerprint):
        raise TypeError("value must be Fingerprint")
    return b"".join(
        (
            _frame(value.algorithm.encode("ascii")),
            encode_varint(value.schema),
            value.digest,
        )
    )


def document_fingerprint(document: OntologyDocument) -> Fingerprint:
    """Return the canonical document fingerprint frozen by model schema 1."""

    return Fingerprint("sha256", 1, hashlib.sha256(document_fingerprint_bytes(document)).digest())


def document_fingerprint_bytes(document: OntologyDocument) -> bytes:
    """Encode document semantic structure, excluding syntax/acquisition metadata."""

    pieces: list[bytes] = [b"pyowl-core:document-fingerprint:v1\x00"]
    for iri in (document.ontology_id.ontology_iri, document.ontology_id.version_iri):
        if iri is None:
            pieces.append(b"0")
        else:
            encoded = canonical_bytes(iri)
            pieces.append(b"1" + _frame(encoded))
    for collection in (
        document.direct_imports,
        document.ontology_annotations,
        document.axioms,
        document.extension_components,
    ):
        pieces.extend(_collection_parts(collection))
    return b"".join(pieces)


def snapshot_structural_fingerprint(
    manifest: ImportManifest,
    documents: Iterable[
        tuple[
            str,
            Iterable[Annotation],
            Iterable[AxiomNode],
            Iterable[StructuralNode],
        ]
    ],
) -> Fingerprint:
    """Fingerprint one resolved snapshot while retaining its document boundaries."""

    hasher = hashlib.sha256()
    hasher.update(b"pyowl-core:snapshot-structural:v1\x00")
    hasher.update(_frame(manifest.canonical_bytes()))
    for key, annotation_values, axioms, extensions in documents:
        hasher.update(_frame(key.encode("ascii")))
        _update_collection(hasher, annotation_values)
        _update_collection(hasher, axioms)
        _update_collection(hasher, extensions)
    return Fingerprint("sha256", 1, hasher.digest())


def effective_structural_fingerprint(
    context: StructuralContext,
    annotations: Iterable[Annotation],
    axioms: Iterable[AxiomNode],
    extensions: Iterable[StructuralNode],
) -> Fingerprint:
    """Fingerprint effective overlay/composite content and canonical boundaries."""

    if not isinstance(context, StructuralContext):
        raise TypeError("context must be StructuralContext")
    domain = {
        StructuralContextKind.OVERLAY: b"pyowl-core:overlay-structural:v1\x00",
        StructuralContextKind.COMPOSITE: b"pyowl-core:composite-structural:v1\x00",
    }[context.kind]
    hasher = hashlib.sha256()
    hasher.update(domain)
    hasher.update(_frame(context.canonical_bytes()))
    _update_collection(hasher, annotations)
    _update_collection(hasher, axioms)
    _update_collection(hasher, extensions)
    return Fingerprint("sha256", 1, hasher.digest())


def logical_fingerprint(
    axioms: Iterable[AxiomNode], extensions: Iterable[StructuralNode]
) -> Fingerprint:
    """Fingerprint the annotation-free logical set and logical extensions."""

    logical: dict[bytes, None] = {}
    for item in axioms:
        if isinstance(item, LOGICAL_AXIOM_TYPES):
            logical[canonical_bytes(without_axiom_annotations(item))] = None
    extension_values = tuple(
        sorted({canonical_bytes(without_annotations(item)) for item in extensions})
    )
    pieces = [
        b"pyowl-core:snapshot-logical:v1\x00",
        b"datatype-policy:owl2-v1\x00",
        encode_varint(len(logical)),
    ]
    pieces.extend(_frame(item) for item in sorted(logical))
    pieces.append(encode_varint(len(extension_values)))
    pieces.extend(b"E" + _frame(item) for item in extension_values)
    return Fingerprint("sha256", 1, hashlib.sha256(b"".join(pieces)).digest())


def signature_fingerprint(
    values: Iterable[Entity], *, include_builtins: bool = True
) -> Fingerprint:
    """Fingerprint one canonical effective entity signature."""

    if not isinstance(include_builtins, bool):
        raise TypeError("include_builtins must be bool")
    members = sorted({canonical_bytes(item) for item in values})
    pieces = [
        b"pyowl-core:snapshot-signature:v1\x00",
        bytes((int(include_builtins),)),
        encode_varint(len(members)),
    ]
    pieces.extend(_frame(item) for item in members)
    return Fingerprint("sha256", 1, hashlib.sha256(b"".join(pieces)).digest())


def without_axiom_annotations(value: AxiomNode) -> AxiomNode:
    """Return the logical form of an axiom without changing any other field."""

    return cast(AxiomNode, without_annotations(value))


def without_annotations(value: StructuralNode) -> StructuralNode:
    """Return a structural value with its top-level annotation set removed."""

    if not is_dataclass(value) or not hasattr(value, "annotations"):
        return value
    annotations = value.annotations
    if isinstance(annotations, CanonicalSet) and not annotations:
        return value
    arguments = {item.name: getattr(value, item.name) for item in fields(value)}
    arguments["annotations"] = CanonicalSet()
    return cast(StructuralNode, type(value)(**arguments))


def _collection_parts(values: Iterable[StructuralNode]) -> list[bytes]:
    encoded = tuple(canonical_bytes(item) for item in values)
    return [encode_varint(len(encoded)), *(_frame(item) for item in encoded)]


def _update_collection(
    hasher: _HashWriter,
    values: Iterable[StructuralNode],
) -> None:
    if hasattr(values, "__len__"):
        size = len(values)  # type: ignore[arg-type]
        hasher.update(encode_varint(size))
        for item in values:
            hasher.update(_frame(canonical_bytes(item)))
        return
    encoded = tuple(canonical_bytes(item) for item in values)
    hasher.update(encode_varint(len(encoded)))
    for encoded_item in encoded:
        hasher.update(_frame(encoded_item))


def _frame(value: bytes) -> bytes:
    return encode_varint(len(value)) + value


__all__ = [
    "StructuralContext",
    "StructuralContextKind",
    "document_fingerprint",
    "document_fingerprint_bytes",
    "effective_structural_fingerprint",
    "fingerprint_bytes",
    "logical_fingerprint",
    "signature_fingerprint",
    "snapshot_structural_fingerprint",
    "without_annotations",
    "without_axiom_annotations",
]
