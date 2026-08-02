"""Independent model-schema-2 anonymous canonicalization oracle.

This intentionally uses only the Python standard library.  It is small and
exhaustive rather than resource-optimized, and exists to catch a shared defect
in the production Python and native implementations.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

_SCOPE_DOMAIN = b"pyowl-core:document-scope:v2\x00"
_KEY_DOMAIN = b"pyowl-core:anonymous-key:v2\x00"
_GRAPH_DOMAIN = b"pyowl-core:blank-graph:v2\x00"
_COLOR_DOMAIN = b"pyowl-core:blank-color:v2\x00"
_COMPONENT_CLASS_DOMAIN = b"pyowl-core:blank-component-class:v2\x00"
_COMPONENT_MANIFEST_DOMAIN = b"pyowl-core:blank-component-manifest:v2\x00"


@dataclass(frozen=True, slots=True)
class ReferenceArc:
    source: str
    role: str
    target: str | None = None
    payload: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source must be a nonempty string")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("role must be a nonempty string")
        self.role.encode("utf-8")
        if self.target is not None and (not isinstance(self.target, str) or not self.target):
            raise ValueError("target must be a nonempty string or None")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload must be bytes")


@dataclass(frozen=True, slots=True)
class ReferenceBinding:
    source_label: str
    canonical_index: int
    occurrence_ordinal: int
    component_graph: bytes
    document_scope: bytes
    local_key: bytes


@dataclass(frozen=True, slots=True)
class ReferenceDocument:
    document_scope: bytes
    component_manifest: bytes
    component_graphs: tuple[bytes, ...]
    bindings: tuple[ReferenceBinding, ...]


@dataclass(frozen=True, slots=True)
class _Component:
    labels: tuple[str, ...]
    arcs: tuple[ReferenceArc, ...]


@dataclass(frozen=True, slots=True)
class _SolvedComponent:
    labels: tuple[str, ...]
    canonical_order: tuple[str, ...]
    canonical_graph: bytes


def canonicalize_document(
    ontology_key: bytes,
    arcs: Iterable[ReferenceArc],
    *,
    labels: Iterable[str] = (),
) -> ReferenceDocument:
    """Canonicalize a complete abstract blank graph under schema 2."""

    if not isinstance(ontology_key, bytes) or not ontology_key:
        raise ValueError("ontology_key must be nonempty bytes")
    arc_values = tuple(arcs)
    if not all(isinstance(arc, ReferenceArc) for arc in arc_values):
        raise TypeError("arcs must contain ReferenceArc values")
    label_values = tuple(labels)
    if not all(isinstance(label, str) and label for label in label_values):
        raise TypeError("labels must contain nonempty strings")

    solved = tuple(
        _solve_component(component)
        for component in _partition_components(arc_values, label_values)
    )
    graphs = tuple(sorted(component.canonical_graph for component in solved))
    manifest = component_manifest(graphs)
    scope = document_scope(ontology_key, manifest)

    classes: dict[bytes, list[_SolvedComponent]] = {}
    for component in solved:
        classes.setdefault(component.canonical_graph, []).append(component)
    bindings: list[ReferenceBinding] = []
    for graph in sorted(classes):
        component_class = hashlib.sha256(
            _COMPONENT_CLASS_DOMAIN + _frame(graph)
        ).digest()
        equivalent_components = sorted(classes[graph], key=lambda item: item.labels)
        for ordinal, component in enumerate(equivalent_components):
            for index, source_label in enumerate(component.canonical_order):
                bindings.append(
                    ReferenceBinding(
                        source_label,
                        index,
                        ordinal,
                        graph,
                        scope,
                        hashlib.sha256(
                            _KEY_DOMAIN
                            + scope
                            + component_class
                            + _varint(ordinal)
                            + _varint(index)
                        ).digest(),
                    )
                )
    return ReferenceDocument(scope, manifest, graphs, tuple(bindings))


def component_manifest(component_graphs: Iterable[bytes]) -> bytes:
    """Return the sorted multiplicity-preserving component manifest."""

    counts: dict[bytes, int] = {}
    for graph in component_graphs:
        if not isinstance(graph, bytes):
            raise TypeError("component graphs must be bytes")
        if not graph:
            raise ValueError("component graphs must be nonempty")
        counts[graph] = counts.get(graph, 0) + 1
    encoded = [_COMPONENT_MANIFEST_DOMAIN, _varint(len(counts))]
    for graph in sorted(counts):
        encoded.extend((_frame(graph), _varint(counts[graph])))
    return b"".join(encoded)


def document_scope(ontology_key: bytes, manifest: bytes) -> bytes:
    """Return the schema-2 document scope for a complete manifest."""

    if not isinstance(ontology_key, bytes) or not ontology_key:
        raise ValueError("ontology_key must be nonempty bytes")
    if not isinstance(manifest, bytes):
        raise TypeError("manifest must be bytes")
    return hashlib.sha256(_SCOPE_DOMAIN + _frame(ontology_key) + _frame(manifest)).digest()


def _partition_components(
    arcs: tuple[ReferenceArc, ...],
    explicit_labels: tuple[str, ...],
) -> tuple[_Component, ...]:
    all_labels = set(explicit_labels)
    for arc in arcs:
        all_labels.add(arc.source)
        if arc.target is not None:
            all_labels.add(arc.target)
    parent = {label: label for label in all_labels}

    def find(label: str) -> str:
        root = label
        while parent[root] != root:
            root = parent[root]
        while parent[label] != label:
            following = parent[label]
            parent[label] = root
            label = following
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    for arc in arcs:
        if arc.target is not None:
            union(arc.source, arc.target)

    grouped_labels: dict[str, list[str]] = {}
    for label in sorted(all_labels):
        grouped_labels.setdefault(find(label), []).append(label)
    grouped_arcs: dict[str, list[ReferenceArc]] = {
        leader: [] for leader in grouped_labels
    }
    for arc in arcs:
        grouped_arcs[find(arc.source)].append(arc)
    return tuple(
        _Component(tuple(grouped_labels[leader]), tuple(grouped_arcs[leader]))
        for leader in sorted(grouped_labels, key=lambda item: tuple(grouped_labels[item]))
    )


def _solve_component(component: _Component) -> _SolvedComponent:
    labels = component.labels
    if not labels:
        raise ValueError("a component must contain a label")
    colors = _colors(_neighborhoods(labels, component.arcs, None))
    while True:
        neighborhoods = _neighborhoods(labels, component.arcs, colors)
        signatures = {
            label: (colors[label], *neighborhoods[label]) for label in labels
        }
        refined = _colors(signatures)
        if _equivalent_partition(labels, colors, refined):
            colors = refined
            break
        colors = refined

    color_classes: dict[bytes, list[str]] = {}
    for label in labels:
        color_classes.setdefault(colors[label], []).append(label)
    partitions = tuple(
        tuple(color_classes[color]) for color in sorted(color_classes)
    )
    best_graph: bytes | None = None
    best_order: tuple[str, ...] | None = None
    for order in _orders(partitions):
        graph = _serialize(order, component.arcs)
        if best_graph is None or graph < best_graph:
            best_graph = graph
            best_order = order
    if best_graph is None or best_order is None:
        raise AssertionError("reference canonicalizer produced no candidate")
    return _SolvedComponent(labels, best_order, best_graph)


def _neighborhoods(
    labels: tuple[str, ...],
    arcs: tuple[ReferenceArc, ...],
    colors: Mapping[str, bytes] | None,
) -> dict[str, tuple[bytes, ...]]:
    values: dict[str, list[bytes]] = {label: [] for label in labels}
    for arc in arcs:
        values[arc.source].append(_arc_view(arc.source, arc, colors))
        if arc.target is not None and arc.target != arc.source:
            values[arc.target].append(_arc_view(arc.target, arc, colors))
    return {label: tuple(sorted(values[label])) for label in labels}


def _arc_view(
    label: str,
    arc: ReferenceArc,
    colors: Mapping[str, bytes] | None,
) -> bytes:
    if arc.source == label:
        direction = b"S"
        if arc.target is None:
            neighbor = b"N"
        elif arc.target == label:
            neighbor = b"L"
        else:
            neighbor = b"B" if colors is None else b"C" + colors[arc.target]
    elif arc.target == label:
        direction = b"T"
        neighbor = b"B" if colors is None else b"C" + colors[arc.source]
    else:
        raise ValueError("label is not incident to arc")
    return direction + _frame(arc.role.encode("utf-8")) + neighbor + _frame(arc.payload)


def _colors(signatures: Mapping[str, tuple[bytes, ...]]) -> dict[str, bytes]:
    return {
        label: hashlib.sha256(
            _COLOR_DOMAIN + b"".join(_frame(item) for item in signature)
        ).digest()
        for label, signature in signatures.items()
    }


def _equivalent_partition(
    labels: tuple[str, ...],
    left: Mapping[str, bytes],
    right: Mapping[str, bytes],
) -> bool:
    forward: dict[bytes, bytes] = {}
    reverse: dict[bytes, bytes] = {}
    for label in labels:
        if forward.setdefault(left[label], right[label]) != right[label]:
            return False
        if reverse.setdefault(right[label], left[label]) != left[label]:
            return False
    return True


def _orders(partitions: tuple[tuple[str, ...], ...]) -> Iterator[tuple[str, ...]]:
    for selection in itertools.product(
        *(itertools.permutations(partition) for partition in partitions)
    ):
        yield tuple(label for part in selection for label in part)


def _serialize(order: tuple[str, ...], arcs: tuple[ReferenceArc, ...]) -> bytes:
    positions = {label: index for index, label in enumerate(order)}
    encoded_arcs: set[bytes] = set()
    for arc in arcs:
        target = (
            b"\x00"
            if arc.target is None
            else b"\x01" + _varint(positions[arc.target])
        )
        encoded_arcs.add(
            _varint(positions[arc.source])
            + _frame(arc.role.encode("utf-8"))
            + target
            + _frame(arc.payload)
        )
    members = sorted(encoded_arcs)
    return (
        _GRAPH_DOMAIN
        + _varint(len(order))
        + _varint(len(members))
        + b"".join(_frame(member) for member in members)
    )


def _frame(value: bytes) -> bytes:
    return _varint(len(value)) + value


def _varint(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("varint value must be a nonnegative integer")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


__all__ = [
    "ReferenceArc",
    "ReferenceBinding",
    "ReferenceDocument",
    "canonicalize_document",
    "component_manifest",
    "document_scope",
]
