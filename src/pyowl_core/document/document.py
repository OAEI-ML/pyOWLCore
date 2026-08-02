"""Immutable one-document OWL structure and document-scoped identity."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import Literal as TypingLiteral
from typing import TypeVar, cast

from pyowl_core.diagnostics import Diagnostic
from pyowl_core.exceptions import ResourceLimitError
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
from pyowl_core.model.anonymous import (
    AlphaCanonicalization,
    _bind_component_blank_nodes,
    _canonical_component_manifest,
)
from pyowl_core.model.axioms import AxiomNode
from pyowl_core.model.visitor import _collect_signature

from .provenance import DocumentProvenance, OriginIndex, RDFMappingReport, SourceMap

A = TypeVar("A", bound=AxiomNode)
_LEXICAL_KEY = b"pyowl-core:parser-blank-label:v2\x00"
_PROVISIONAL_SCOPE = hashlib.sha256(b"pyowl-core:provisional-document-scope:v2\x00").digest()


@dataclass(frozen=True, slots=True)
class _BlankComponent:
    labels: tuple[str, ...]
    arcs: tuple[BlankNodeArc, ...]
    root_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _BlankPartitionSummary:
    component_count: int
    largest_component_labels: int
    largest_component_arcs: int
    largest_component_roots: int
    maximum_root_interval_span: int
    maximum_open_root_intervals: int
    total_labels: int
    total_arcs: int


@dataclass(frozen=True, slots=True)
class _BlankComponentTelemetry:
    label_count: int
    arc_count: int
    root_count: int
    setup_work: int
    refinement_work: int
    candidate_order_work: int
    canonical_work: int
    refinement_rounds: int
    permutations_examined: int


@dataclass(frozen=True, slots=True)
class _BlankCanonicalizationSummary:
    components: tuple[_BlankComponentTelemetry, ...]
    total_setup_work: int
    total_refinement_work: int
    total_candidate_order_work: int
    total_canonical_work: int
    largest_component_work: int
    maximum_refinement_rounds: int
    total_permutations_examined: int


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
        gathered = _collect_signature(
            (*self.ontology_annotations, *self.axioms, *self.extension_components)
        )
        if not include_builtins:
            gathered = tuple(item for item in gathered if not _is_builtin(item))
        if kind is not None:
            gathered = tuple(item for item in gathered if item.kind is kind)
        return gathered

    @property
    def document_fingerprint(self) -> Fingerprint:
        from .fingerprint import document_fingerprint

        return document_fingerprint(self)

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
    components, summary = _partition_blank_graph(roots, limits=limits)
    if not components:
        return (
            imports,
            CanonicalSet(annotations),
            CanonicalSet(axiom_values),
            CanonicalSet(extensions),
        )

    solved, _ = _canonicalize_blank_components(
        components,
        summary,
        limits=limits,
    )

    manifest = _canonical_component_manifest(
        canonicalization.canonical_graph for _, canonicalization in solved
    )
    ontology_key = _ontology_key(ontology_id)
    scope = canonical_document_scope(ontology_key, canonical_graph=manifest)
    classes: dict[bytes, list[tuple[_BlankComponent, AlphaCanonicalization]]] = {}
    for component, canonicalization in solved:
        classes.setdefault(canonicalization.canonical_graph, []).append(
            (component, canonicalization)
        )
    replacements: dict[str, AnonymousIndividual] = {}
    for graph in sorted(classes):
        component_class = sorted(classes[graph], key=lambda item: item[0].labels)
        for occurrence_ordinal, (_, canonicalization) in enumerate(component_class):
            rebound = _bind_component_blank_nodes(
                canonicalization,
                scope,
                occurrence_ordinal=occurrence_ordinal,
            )
            replacements.update(rebound.as_mapping())
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


def _partition_blank_graph(
    roots: tuple[StructuralNode, ...],
    *,
    limits: object | None = None,
) -> tuple[tuple[_BlankComponent, ...], _BlankPartitionSummary]:
    """Partition provisional blanks and expose bounded structural telemetry."""

    maximum_terms = _blank_limit(limits, "max_terms", 500_000_000)
    parents: dict[str, str] = {}
    ranks: dict[str, int] = {}
    component_arcs: dict[str, list[BlankNodeArc]] = {}
    component_roots: dict[str, list[int]] = {}
    arc_count = 0

    def add_label(label: str) -> None:
        if label not in parents:
            parents[label] = label
            ranks[label] = 0
            component_arcs[label] = []
            component_roots[label] = []
            _enforce_blank_terms(len(parents) + arc_count, maximum_terms)

    def find(label: str) -> str:
        parent = parents[label]
        while parent != parents[parent]:
            parent = parents[parent]
        while label != parent:
            next_label = parents[label]
            parents[label] = parent
            label = next_label
        return parent

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        left_rank = ranks[left_root]
        right_rank = ranks[right_root]
        if left_rank < right_rank:
            left_root, right_root = right_root, left_root
        parents[right_root] = left_root
        component_arcs[left_root].extend(component_arcs.pop(right_root))
        component_roots[left_root].extend(component_roots.pop(right_root))
        if left_rank == right_rank:
            ranks[left_root] += 1

    def append_arc(leader: str, arc: BlankNodeArc) -> None:
        nonlocal arc_count
        component_arcs[leader].append(arc)
        arc_count += 1
        _enforce_blank_terms(len(parents) + arc_count, maximum_terms)

    for root_index, root in enumerate(roots):
        skeleton = _skeleton(root)
        occurrences = tuple(_blank_occurrences(root, (type(root).__name__,)))
        if not occurrences:
            continue
        labels = tuple(sorted({label for label, _ in occurrences}))
        for label in labels:
            add_label(label)
        for label in labels[1:]:
            union(labels[0], label)
        leader = find(labels[0])
        component_roots[leader].append(root_index)
        for label, path in occurrences:
            append_arc(leader, BlankNodeArc(label, "/".join(path), payload=skeleton))
        for index, (source, source_path) in enumerate(occurrences):
            for target, target_path in occurrences[index + 1 :]:
                append_arc(
                    leader,
                    BlankNodeArc(
                        source,
                        "/".join(source_path) + "->" + "/".join(target_path),
                        target,
                        skeleton,
                    ),
                )

    if not parents:
        return (), _BlankPartitionSummary(0, 0, 0, 0, 0, 0, 0, 0)

    grouped_labels: dict[str, list[str]] = {}
    for label in sorted(parents):
        grouped_labels.setdefault(find(label), []).append(label)

    components = tuple(
        sorted(
            (
                _BlankComponent(
                    tuple(labels),
                    tuple(component_arcs[leader]),
                    tuple(sorted(component_roots[leader])),
                )
                for leader, labels in grouped_labels.items()
            ),
            key=lambda component: component.labels,
        )
    )
    intervals = tuple(
        (component.root_indexes[0], component.root_indexes[-1]) for component in components
    )
    events: dict[int, int] = {}
    for start, end in intervals:
        events[start] = events.get(start, 0) + 1
        events[end + 1] = events.get(end + 1, 0) - 1
    active = 0
    maximum_active = 0
    for index in sorted(events):
        active += events[index]
        maximum_active = max(maximum_active, active)
    return components, _BlankPartitionSummary(
        component_count=len(components),
        largest_component_labels=max(len(component.labels) for component in components),
        largest_component_arcs=max(len(component.arcs) for component in components),
        largest_component_roots=max(len(component.root_indexes) for component in components),
        maximum_root_interval_span=max(end - start + 1 for start, end in intervals),
        maximum_open_root_intervals=maximum_active,
        total_labels=len(parents),
        total_arcs=arc_count,
    )


def _canonicalize_blank_components(
    components: tuple[_BlankComponent, ...],
    partition: _BlankPartitionSummary,
    *,
    limits: object | None = None,
) -> tuple[
    list[tuple[_BlankComponent, AlphaCanonicalization]],
    _BlankCanonicalizationSummary,
]:
    """Run each component once and retain per-phase benchmark telemetry."""

    solved: list[tuple[_BlankComponent, AlphaCanonicalization]] = []
    telemetry: list[_BlankComponentTelemetry] = []
    for component in components:
        try:
            canonicalization = alpha_canonicalize_blank_nodes(
                component.arcs,
                _PROVISIONAL_SCOPE,
                labels=component.labels,
                limits=limits,
            )
        except ResourceLimitError as error:
            if error.limit != "max_canonical_work":
                raise
            details = dict(getattr(error, "details", {}))
            details.update(
                {
                    "component_count": partition.component_count,
                    "largest_component_labels": partition.largest_component_labels,
                    "largest_component_arcs": partition.largest_component_arcs,
                }
            )
            raise ResourceLimitError(
                str(error),
                limit=error.limit,
                observed=error.observed,
                allowed=error.allowed,
                code=error.code,
                details=details,
            ) from error
        solved.append((component, canonicalization))
        setup_work = len(component.labels) + 2 * len(component.arcs)
        refinement_work = canonicalization.refinement_rounds * (
            2 * len(component.labels) + 2 * len(component.arcs)
        )
        candidate_order_work = canonicalization.permutations_examined * max(
            1,
            len(component.labels) + len(component.arcs),
        )
        telemetry.append(
            _BlankComponentTelemetry(
                label_count=len(component.labels),
                arc_count=len(component.arcs),
                root_count=len(component.root_indexes),
                setup_work=setup_work,
                refinement_work=refinement_work,
                candidate_order_work=candidate_order_work,
                canonical_work=setup_work + refinement_work + candidate_order_work,
                refinement_rounds=canonicalization.refinement_rounds,
                permutations_examined=canonicalization.permutations_examined,
            )
        )
    entries = tuple(telemetry)
    return solved, _BlankCanonicalizationSummary(
        components=entries,
        total_setup_work=sum(item.setup_work for item in entries),
        total_refinement_work=sum(item.refinement_work for item in entries),
        total_candidate_order_work=sum(item.candidate_order_work for item in entries),
        total_canonical_work=sum(item.canonical_work for item in entries),
        largest_component_work=max(
            (item.canonical_work for item in entries),
            default=0,
        ),
        maximum_refinement_rounds=max(
            (item.refinement_rounds for item in entries),
            default=0,
        ),
        total_permutations_examined=sum(item.permutations_examined for item in entries),
    )


def _blank_component_summary(
    roots: tuple[StructuralNode, ...],
    *,
    limits: object | None = None,
) -> _BlankPartitionSummary:
    """Return the component metrics consumed by benchmarks and regression tests."""

    return _partition_blank_graph(roots, limits=limits)[1]


def _blank_limit(limits: object | None, name: str, default: int) -> int:
    value = default if limits is None else getattr(limits, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _enforce_blank_terms(observed: int, maximum: int) -> None:
    if observed > maximum:
        raise ResourceLimitError(
            "resource limit max_terms exceeded",
            limit="max_terms",
            observed=observed,
            allowed=maximum,
        )


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
            marker = hashlib.sha256(_skeleton(item)).hexdigest()
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
    from .fingerprint import document_fingerprint_bytes

    return document_fingerprint_bytes(document)


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
