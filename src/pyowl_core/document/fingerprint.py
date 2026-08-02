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
        """Return the active model-schema-2 structural-context preimage."""

        return _structural_context_bytes(self, version=2)


def _structural_context_bytes(context: StructuralContext, *, version: int) -> bytes:
    if version not in {1, 2}:
        raise ValueError("fingerprint schema version must be 1 or 2")
    domain = f"pyowl-core:view-structure-context:v{version}\x00".encode("ascii")
    pieces = [
        domain,
        _frame(context.kind.value.encode("ascii")),
        encode_varint(len(context.fingerprints)),
    ]
    pieces.extend(_frame(fingerprint_bytes(item)) for item in context.fingerprints)
    return b"".join(pieces)


def structural_context_bytes_v1(context: StructuralContext) -> bytes:
    """Return the frozen model-schema-1 structural-context preimage."""

    if not isinstance(context, StructuralContext):
        raise TypeError("context must be StructuralContext")
    return _structural_context_bytes(context, version=1)


def structural_context_bytes_v2(context: StructuralContext) -> bytes:
    """Return the active model-schema-2 structural-context preimage."""

    if not isinstance(context, StructuralContext):
        raise TypeError("context must be StructuralContext")
    return _structural_context_bytes(context, version=2)


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
    """Return the canonical document fingerprint for model schema 2."""

    return Fingerprint("sha256", 2, hashlib.sha256(document_fingerprint_bytes(document)).digest())


def document_fingerprint_bytes(document: OntologyDocument) -> bytes:
    """Encode model-schema-2 document structure, excluding acquisition metadata."""

    return _document_fingerprint_bytes(document, version=2)


def document_fingerprint_v1(document: OntologyDocument) -> Fingerprint:
    """Return the frozen model-schema-1 document fingerprint."""

    return Fingerprint(
        "sha256",
        1,
        hashlib.sha256(document_fingerprint_bytes_v1(document)).digest(),
    )


def document_fingerprint_bytes_v1(document: OntologyDocument) -> bytes:
    """Encode the frozen model-schema-1 document fingerprint preimage."""

    return _document_fingerprint_bytes(document, version=1)


def _document_fingerprint_bytes(document: OntologyDocument, *, version: int) -> bytes:
    if version not in {1, 2}:
        raise ValueError("fingerprint schema version must be 1 or 2")

    domain = f"pyowl-core:document-fingerprint:v{version}\x00".encode("ascii")
    pieces: list[bytes] = [domain]
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

    return Fingerprint(
        "sha256",
        2,
        _snapshot_structural_fingerprint_digest(manifest, documents, version=2),
    )


def snapshot_structural_fingerprint_bytes(
    manifest: ImportManifest,
    documents: Iterable[
        tuple[
            str,
            Iterable[Annotation],
            Iterable[AxiomNode],
            Iterable[StructuralNode],
        ]
    ],
) -> bytes:
    """Return the authoritative snapshot-structural fingerprint preimage."""

    return _snapshot_structural_fingerprint_bytes(manifest, documents, version=2)


def snapshot_structural_fingerprint_v1(
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
    """Return the frozen model-schema-1 snapshot-structural fingerprint."""

    return Fingerprint(
        "sha256",
        1,
        _snapshot_structural_fingerprint_digest(manifest, documents, version=1),
    )


def snapshot_structural_fingerprint_bytes_v1(
    manifest: ImportManifest,
    documents: Iterable[
        tuple[
            str,
            Iterable[Annotation],
            Iterable[AxiomNode],
            Iterable[StructuralNode],
        ]
    ],
) -> bytes:
    """Return the frozen model-schema-1 snapshot-structural preimage."""

    return _snapshot_structural_fingerprint_bytes(manifest, documents, version=1)


def _snapshot_structural_fingerprint_bytes(
    manifest: ImportManifest,
    documents: Iterable[
        tuple[
            str,
            Iterable[Annotation],
            Iterable[AxiomNode],
            Iterable[StructuralNode],
        ]
    ],
    *,
    version: int,
) -> bytes:
    if version not in {1, 2}:
        raise ValueError("fingerprint schema version must be 1 or 2")

    pieces = [
        f"pyowl-core:snapshot-structural:v{version}\x00".encode("ascii"),
        _frame(manifest.canonical_bytes()),
    ]
    for key, annotation_values, axioms, extensions in documents:
        pieces.append(_frame(key.encode("ascii")))
        pieces.extend(_collection_parts(annotation_values))
        pieces.extend(_collection_parts(axioms))
        pieces.extend(_collection_parts(extensions))
    return b"".join(pieces)


def _snapshot_structural_fingerprint_digest(
    manifest: ImportManifest,
    documents: Iterable[
        tuple[
            str,
            Iterable[Annotation],
            Iterable[AxiomNode],
            Iterable[StructuralNode],
        ]
    ],
    *,
    version: int,
) -> bytes:
    if version not in {1, 2}:
        raise ValueError("fingerprint schema version must be 1 or 2")
    hasher = hashlib.sha256()
    hasher.update(f"pyowl-core:snapshot-structural:v{version}\x00".encode("ascii"))
    hasher.update(_frame(manifest.canonical_bytes()))
    for key, annotation_values, axioms, extensions in documents:
        hasher.update(_frame(key.encode("ascii")))
        _update_collection(hasher, annotation_values)
        _update_collection(hasher, axioms)
        _update_collection(hasher, extensions)
    return hasher.digest()


def effective_structural_fingerprint(
    context: StructuralContext,
    annotations: Iterable[Annotation],
    axioms: Iterable[AxiomNode],
    extensions: Iterable[StructuralNode],
) -> Fingerprint:
    """Fingerprint effective overlay/composite content and canonical boundaries."""

    return _effective_structural_fingerprint(
        context,
        annotations,
        axioms,
        extensions,
        version=2,
    )


def effective_structural_fingerprint_v1(
    context: StructuralContext,
    annotations: Iterable[Annotation],
    axioms: Iterable[AxiomNode],
    extensions: Iterable[StructuralNode],
) -> Fingerprint:
    """Return the frozen model-schema-1 effective structural fingerprint."""

    return _effective_structural_fingerprint(
        context,
        annotations,
        axioms,
        extensions,
        version=1,
    )


def _effective_structural_fingerprint(
    context: StructuralContext,
    annotations: Iterable[Annotation],
    axioms: Iterable[AxiomNode],
    extensions: Iterable[StructuralNode],
    *,
    version: int,
) -> Fingerprint:
    if not isinstance(context, StructuralContext):
        raise TypeError("context must be StructuralContext")
    if version not in {1, 2}:
        raise ValueError("fingerprint schema version must be 1 or 2")
    domain = {
        StructuralContextKind.OVERLAY: (
            f"pyowl-core:overlay-structural:v{version}\x00".encode("ascii")
        ),
        StructuralContextKind.COMPOSITE: (
            f"pyowl-core:composite-structural:v{version}\x00".encode("ascii")
        ),
    }[context.kind]
    hasher = hashlib.sha256()
    hasher.update(domain)
    hasher.update(_frame(_structural_context_bytes(context, version=version)))
    _update_collection(hasher, annotations)
    _update_collection(hasher, axioms)
    _update_collection(hasher, extensions)
    return Fingerprint("sha256", version, hasher.digest())


def logical_fingerprint(
    axioms: Iterable[AxiomNode], extensions: Iterable[StructuralNode]
) -> Fingerprint:
    """Fingerprint the annotation-free logical set and logical extensions."""

    preimage = logical_fingerprint_bytes(axioms, extensions)
    return Fingerprint("sha256", 2, hashlib.sha256(preimage).digest())


def logical_fingerprint_v1(
    axioms: Iterable[AxiomNode], extensions: Iterable[StructuralNode]
) -> Fingerprint:
    """Return the frozen model-schema-1 logical fingerprint."""

    preimage = logical_fingerprint_bytes_v1(axioms, extensions)
    return Fingerprint("sha256", 1, hashlib.sha256(preimage).digest())


def logical_fingerprint_bytes(
    axioms: Iterable[AxiomNode], extensions: Iterable[StructuralNode]
) -> bytes:
    """Return the authoritative model-schema-2 logical fingerprint preimage."""

    return _logical_fingerprint_bytes(axioms, extensions, version=2)


def logical_fingerprint_bytes_v1(
    axioms: Iterable[AxiomNode], extensions: Iterable[StructuralNode]
) -> bytes:
    """Return the frozen model-schema-1 logical fingerprint preimage."""

    return _logical_fingerprint_bytes(axioms, extensions, version=1)


def _logical_fingerprint_bytes(
    axioms: Iterable[AxiomNode],
    extensions: Iterable[StructuralNode],
    *,
    version: int,
) -> bytes:
    if version not in {1, 2}:
        raise ValueError("fingerprint schema version must be 1 or 2")

    logical = {
        canonical_bytes(without_axiom_annotations(item))
        for item in axioms
        if isinstance(item, LOGICAL_AXIOM_TYPES)
    }
    extension_values = tuple(
        sorted({canonical_bytes(without_annotations(item)) for item in extensions})
    )
    pieces = [
        f"pyowl-core:snapshot-logical:v{version}\x00".encode("ascii"),
        b"datatype-policy:owl2-v1\x00",
        encode_varint(len(logical)),
    ]
    pieces.extend(_frame(item) for item in sorted(logical))
    pieces.append(encode_varint(len(extension_values)))
    pieces.extend(b"E" + _frame(item) for item in extension_values)
    return b"".join(pieces)


def signature_fingerprint(
    values: Iterable[Entity], *, include_builtins: bool = True
) -> Fingerprint:
    """Fingerprint one canonical effective entity signature."""

    preimage = signature_fingerprint_bytes(values, include_builtins=include_builtins)
    return Fingerprint("sha256", 2, hashlib.sha256(preimage).digest())


def signature_fingerprint_bytes(
    values: Iterable[Entity], *, include_builtins: bool = True
) -> bytes:
    """Return the authoritative model-schema-2 signature preimage."""

    return _signature_fingerprint_bytes(values, include_builtins=include_builtins, version=2)


def signature_fingerprint_v1(
    values: Iterable[Entity], *, include_builtins: bool = True
) -> Fingerprint:
    """Return the frozen model-schema-1 signature fingerprint."""

    preimage = signature_fingerprint_bytes_v1(values, include_builtins=include_builtins)
    return Fingerprint("sha256", 1, hashlib.sha256(preimage).digest())


def signature_fingerprint_bytes_v1(
    values: Iterable[Entity], *, include_builtins: bool = True
) -> bytes:
    """Return the frozen model-schema-1 signature fingerprint preimage."""

    return _signature_fingerprint_bytes(values, include_builtins=include_builtins, version=1)


def _signature_fingerprint_bytes(
    values: Iterable[Entity],
    *,
    include_builtins: bool,
    version: int,
) -> bytes:

    if not isinstance(include_builtins, bool):
        raise TypeError("include_builtins must be bool")
    if version not in {1, 2}:
        raise ValueError("fingerprint schema version must be 1 or 2")
    members = sorted({canonical_bytes(item) for item in values})
    pieces = [
        f"pyowl-core:snapshot-signature:v{version}\x00".encode("ascii"),
        bytes((int(include_builtins),)),
        encode_varint(len(members)),
    ]
    pieces.extend(_frame(item) for item in members)
    return b"".join(pieces)


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
    "document_fingerprint_bytes_v1",
    "document_fingerprint_v1",
    "effective_structural_fingerprint",
    "effective_structural_fingerprint_v1",
    "fingerprint_bytes",
    "logical_fingerprint",
    "logical_fingerprint_bytes",
    "logical_fingerprint_bytes_v1",
    "logical_fingerprint_v1",
    "signature_fingerprint",
    "signature_fingerprint_bytes",
    "signature_fingerprint_bytes_v1",
    "signature_fingerprint_v1",
    "snapshot_structural_fingerprint",
    "snapshot_structural_fingerprint_bytes",
    "snapshot_structural_fingerprint_bytes_v1",
    "snapshot_structural_fingerprint_v1",
    "structural_context_bytes_v1",
    "structural_context_bytes_v2",
    "without_annotations",
    "without_axiom_annotations",
]
