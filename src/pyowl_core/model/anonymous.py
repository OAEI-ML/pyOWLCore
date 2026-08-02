"""Deterministic document-scoped blank-node alpha canonicalization."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from pyowl_core.exceptions import ResourceLimitError, StructuralConstraintError

from .canonical import encode_varint
from .primitives import IRI, AnonymousIndividual
from .provenance import RescopeRecord

_SCOPE_DOMAIN = b"pyowl-core:document-scope:v2\x00"
_KEY_DOMAIN = b"pyowl-core:anonymous-key:v2\x00"
_GRAPH_DOMAIN = b"pyowl-core:blank-graph:v2\x00"
_COLOR_DOMAIN = b"pyowl-core:blank-color:v2\x00"
_COMPONENT_CLASS_DOMAIN = b"pyowl-core:blank-component-class:v2\x00"
_COMPONENT_MANIFEST_DOMAIN = b"pyowl-core:blank-component-manifest:v2\x00"


@dataclass(frozen=True, slots=True)
class BlankNodeArc:
    """One role-labelled structural neighborhood occurrence.

    ``payload`` is the already-canonical nonblank portion of the occurrence.
    ``target`` is another blank label, or ``None`` for a unary occurrence.
    Labels are lexical handles only and never enter canonical graph bytes.
    """

    source: str
    role: str
    target: str | None = None
    payload: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("BlankNodeArc.source must be a nonempty string")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("BlankNodeArc.role must be a nonempty string")
        try:
            self.role.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("BlankNodeArc.role must contain Unicode scalar values") from error
        if self.target is not None and (not isinstance(self.target, str) or not self.target):
            raise ValueError("BlankNodeArc.target must be a nonempty string or None")
        if not isinstance(self.payload, bytes):
            raise TypeError("BlankNodeArc.payload must be bytes")


@dataclass(frozen=True, slots=True)
class BlankNodeBinding:
    source_label: str
    canonical_index: int
    individual: AnonymousIndividual

    def __post_init__(self) -> None:
        if not isinstance(self.source_label, str) or not self.source_label:
            raise ValueError("source_label must be a nonempty string")
        if (
            isinstance(self.canonical_index, bool)
            or not isinstance(self.canonical_index, int)
            or self.canonical_index < 0
        ):
            raise ValueError("canonical_index must be a nonnegative integer")
        if not isinstance(self.individual, AnonymousIndividual):
            raise TypeError("individual must be AnonymousIndividual")


@dataclass(frozen=True, slots=True)
class AlphaCanonicalization:
    bindings: tuple[BlankNodeBinding, ...]
    canonical_graph: bytes
    refinement_rounds: int
    permutations_examined: int

    def __post_init__(self) -> None:
        bindings = tuple(self.bindings)
        if not all(isinstance(binding, BlankNodeBinding) for binding in bindings):
            raise TypeError("bindings must contain BlankNodeBinding values")
        if not isinstance(self.canonical_graph, bytes):
            raise TypeError("canonical_graph must be bytes")
        if (
            isinstance(self.refinement_rounds, bool)
            or not isinstance(self.refinement_rounds, int)
            or self.refinement_rounds < 0
        ):
            raise ValueError("refinement_rounds must be a nonnegative integer")
        if (
            isinstance(self.permutations_examined, bool)
            or not isinstance(self.permutations_examined, int)
            or self.permutations_examined < 1
        ):
            raise ValueError("permutations_examined must be a positive integer")
        indexes = tuple(binding.canonical_index for binding in bindings)
        if indexes != tuple(range(len(bindings))):
            raise ValueError("binding indexes must be contiguous and canonical")
        if len({binding.source_label for binding in bindings}) != len(bindings):
            raise ValueError("binding source labels must be unique")
        object.__setattr__(self, "bindings", bindings)

    def individual(self, source_label: str) -> AnonymousIndividual:
        for binding in self.bindings:
            if binding.source_label == source_label:
                return binding.individual
        raise KeyError(source_label)

    def as_mapping(self) -> Mapping[str, AnonymousIndividual]:
        return {binding.source_label: binding.individual for binding in self.bindings}


def canonical_document_scope(
    document_key: IRI | str | bytes,
    *,
    canonical_graph: bytes = b"",
) -> bytes:
    if isinstance(document_key, IRI):
        key = document_key.value.encode("utf-8")
    elif isinstance(document_key, str):
        try:
            key = document_key.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("document_key must contain Unicode scalar values") from error
    elif isinstance(document_key, bytes):
        key = document_key
    else:
        raise TypeError("document_key must be IRI, str, or bytes")
    if not key:
        raise ValueError("document_key must be nonempty")
    if not isinstance(canonical_graph, bytes):
        raise TypeError("canonical_graph must be bytes")
    return hashlib.sha256(
        _SCOPE_DOMAIN
        + encode_varint(len(key))
        + key
        + encode_varint(len(canonical_graph))
        + canonical_graph
    ).digest()


def _canonical_component_manifest(component_graphs: Iterable[bytes]) -> bytes:
    """Encode the sorted, multiplicity-preserving schema-2 component manifest."""

    multiplicities: dict[bytes, int] = {}
    for graph in component_graphs:
        if not isinstance(graph, bytes):
            raise TypeError("component_graphs must contain bytes")
        if not graph:
            raise ValueError("component graph bytes must be nonempty")
        multiplicities[graph] = multiplicities.get(graph, 0) + 1
    pieces = [_COMPONENT_MANIFEST_DOMAIN, encode_varint(len(multiplicities))]
    for graph in sorted(multiplicities):
        pieces.extend(
            (
                encode_varint(len(graph)),
                graph,
                encode_varint(multiplicities[graph]),
            )
        )
    return b"".join(pieces)


def _bind_component_blank_nodes(
    canonicalization: AlphaCanonicalization,
    document_scope: bytes,
    *,
    occurrence_ordinal: int,
) -> AlphaCanonicalization:
    """Bind a cached component order to one multiplicity-preserving output slot."""

    if not isinstance(canonicalization, AlphaCanonicalization):
        raise TypeError("canonicalization must be AlphaCanonicalization")
    if not isinstance(document_scope, bytes) or len(document_scope) != 32:
        raise StructuralConstraintError("document_scope must be exactly 32 bytes")
    if (
        isinstance(occurrence_ordinal, bool)
        or not isinstance(occurrence_ordinal, int)
        or occurrence_ordinal < 0
    ):
        raise ValueError("occurrence_ordinal must be a nonnegative integer")
    order = tuple(binding.source_label for binding in canonicalization.bindings)
    bindings = _bindings_for_order(
        order,
        document_scope,
        canonicalization.canonical_graph,
        occurrence_ordinal,
    )
    return AlphaCanonicalization(
        bindings,
        canonicalization.canonical_graph,
        canonicalization.refinement_rounds,
        canonicalization.permutations_examined,
    )


def alpha_canonicalize_blank_nodes(
    arcs: Iterable[BlankNodeArc],
    document_scope: bytes,
    *,
    labels: Iterable[str] = (),
    limits: object | None = None,
) -> AlphaCanonicalization:
    if not isinstance(document_scope, bytes) or len(document_scope) != 32:
        raise StructuralConstraintError("document_scope must be exactly 32 bytes")
    arc_values = tuple(arcs)
    if not all(isinstance(arc, BlankNodeArc) for arc in arc_values):
        raise TypeError("arcs must contain BlankNodeArc values")
    label_set = set(labels)
    if not all(isinstance(label, str) and label for label in label_set):
        raise TypeError("labels must contain nonempty strings")
    for arc in arc_values:
        label_set.add(arc.source)
        if arc.target is not None:
            label_set.add(arc.target)
    ordered_labels = tuple(sorted(label_set))
    if not ordered_labels:
        return AlphaCanonicalization((), _empty_graph(), 0, 1)

    maximum = _limit(limits, "max_canonical_work", 1_000_000_000)
    maximum_terms = _limit(limits, "max_terms", 500_000_000)
    terms = len(ordered_labels) + len(arc_values)
    if terms > maximum_terms:
        raise ResourceLimitError(
            "resource limit max_terms exceeded",
            limit="max_terms",
            observed=terms,
            allowed=maximum_terms,
        )
    setup_work = len(ordered_labels) + 2 * len(arc_values)
    work = setup_work
    _enforce_work(
        work,
        maximum,
        details={
            "component_count": 1,
            "largest_component_labels": len(ordered_labels),
            "largest_component_arcs": len(arc_values),
            "refinement_rounds": 0,
            "work_term": "setup",
        },
    )
    colors = _initial_colors(ordered_labels, arc_values)
    rounds = 0
    refinement_work = 0
    while True:
        rounds += 1
        refined = _refine_colors(ordered_labels, arc_values, colors)
        round_work = 2 * len(ordered_labels) + 2 * len(arc_values)
        refinement_work += round_work
        work += round_work
        _enforce_work(
            work,
            maximum,
            details={
                "component_count": 1,
                "largest_component_labels": len(ordered_labels),
                "largest_component_arcs": len(arc_values),
                "refinement_rounds": rounds,
                "work_term": "refinement",
            },
        )
        if _same_partition(ordered_labels, colors, refined):
            colors = refined
            break
        colors = refined
        if rounds > len(ordered_labels) + 1:
            raise StructuralConstraintError("blank-node partition refinement did not converge")

    partitions = _partitions(ordered_labels, colors)
    candidate_details: dict[str, str | int | bool] = {
        "component_count": 1,
        "largest_component_labels": len(ordered_labels),
        "largest_component_arcs": len(arc_values),
        "refinement_rounds": rounds,
        "work_term": "candidate_orders",
    }
    candidate_count = _bounded_permutation_count(
        partitions,
        maximum,
        work,
        details=candidate_details,
    )
    candidate_unit_work = max(1, len(ordered_labels) + len(arc_values))
    candidate_order_work = candidate_count * candidate_unit_work
    final_work = work + candidate_order_work
    _enforce_work(final_work, maximum, details=candidate_details)

    best_graph: bytes | None = None
    best_order: tuple[str, ...] | None = None
    examined = 0
    for order in _candidate_orders(partitions):
        examined += 1
        graph = _serialize_graph(order, arc_values)
        if best_graph is None or graph < best_graph:
            best_graph = graph
            best_order = order
    if best_graph is None or best_order is None:
        raise AssertionError("blank-node canonicalization produced no candidate")

    bindings = _bindings_for_order(
        best_order,
        document_scope,
        best_graph,
        0,
    )
    return AlphaCanonicalization(bindings, best_graph, rounds, examined)


def _limit(limits: object | None, name: str, default: int) -> int:
    value = default if limits is None else getattr(limits, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _enforce_work(
    observed: int,
    maximum: int,
    *,
    details: Mapping[str, str | int | bool],
) -> None:
    if observed > maximum:
        raise ResourceLimitError(
            "resource limit max_canonical_work exceeded",
            limit="max_canonical_work",
            observed=observed,
            allowed=maximum,
            details=details,
        )


def _bounded_permutation_count(
    partitions: tuple[tuple[str, ...], ...],
    maximum: int,
    consumed: int,
    *,
    details: Mapping[str, str | int | bool],
) -> int:
    count = 1
    remaining = max(0, maximum - consumed)
    for partition in partitions:
        for factor in range(2, len(partition) + 1):
            if count > remaining // factor:
                _enforce_work(maximum + 1, maximum, details=details)
            count *= factor
    return count


def _empty_graph() -> bytes:
    return _GRAPH_DOMAIN + encode_varint(0) + encode_varint(0)


def _arc_signature(
    label: str,
    arc: BlankNodeArc,
    colors: Mapping[str, bytes] | None,
) -> bytes | None:
    role = arc.role.encode("utf-8")
    payload = arc.payload
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
        return None
    return (
        direction
        + encode_varint(len(role))
        + role
        + neighbor
        + encode_varint(len(payload))
        + payload
    )


def _colors_from_signatures(signatures: Mapping[str, tuple[bytes, ...]]) -> dict[str, bytes]:
    return {
        label: hashlib.sha256(
            _COLOR_DOMAIN + b"".join(encode_varint(len(item)) + item for item in signature)
        ).digest()
        for label, signature in signatures.items()
    }


def _initial_colors(
    labels: tuple[str, ...],
    arcs: tuple[BlankNodeArc, ...],
) -> dict[str, bytes]:
    signatures = _neighborhood_signatures(labels, arcs, None)
    return _colors_from_signatures(signatures)


def _refine_colors(
    labels: tuple[str, ...],
    arcs: tuple[BlankNodeArc, ...],
    colors: Mapping[str, bytes],
) -> dict[str, bytes]:
    neighborhoods = _neighborhood_signatures(labels, arcs, colors)
    signatures = {label: (colors[label], *neighborhoods[label]) for label in labels}
    return _colors_from_signatures(signatures)


def _neighborhood_signatures(
    labels: tuple[str, ...],
    arcs: tuple[BlankNodeArc, ...],
    colors: Mapping[str, bytes] | None,
) -> dict[str, tuple[bytes, ...]]:
    gathered: dict[str, list[bytes]] = {label: [] for label in labels}
    for arc in arcs:
        source_signature = _arc_signature(arc.source, arc, colors)
        if source_signature is None:
            raise AssertionError("source arc signature is missing")
        gathered[arc.source].append(source_signature)
        if arc.target is not None and arc.target != arc.source:
            target_signature = _arc_signature(arc.target, arc, colors)
            if target_signature is None:
                raise AssertionError("target arc signature is missing")
            gathered[arc.target].append(target_signature)
    return {label: tuple(sorted(gathered[label])) for label in labels}


def _same_partition(
    labels: tuple[str, ...],
    first: Mapping[str, bytes],
    second: Mapping[str, bytes],
) -> bool:
    forward: dict[bytes, bytes] = {}
    reverse: dict[bytes, bytes] = {}
    for label in labels:
        first_color = first[label]
        second_color = second[label]
        if forward.setdefault(first_color, second_color) != second_color:
            return False
        if reverse.setdefault(second_color, first_color) != first_color:
            return False
    return True


def _partitions(
    labels: tuple[str, ...],
    colors: Mapping[str, bytes],
) -> tuple[tuple[str, ...], ...]:
    grouped: dict[bytes, list[str]] = {}
    for label in labels:
        grouped.setdefault(colors[label], []).append(label)
    return tuple(tuple(grouped[color]) for color in sorted(grouped))


def _candidate_orders(partitions: tuple[tuple[str, ...], ...]) -> Iterator[tuple[str, ...]]:
    choices = (itertools.permutations(partition) for partition in partitions)
    for candidate in itertools.product(*choices):
        yield tuple(label for partition in candidate for label in partition)


def _serialize_graph(order: tuple[str, ...], arcs: tuple[BlankNodeArc, ...]) -> bytes:
    indexes = {label: index for index, label in enumerate(order)}
    encoded_arcs: set[bytes] = set()
    for arc in arcs:
        role = arc.role.encode("utf-8")
        target = b"\x00" if arc.target is None else b"\x01" + encode_varint(indexes[arc.target])
        encoded_arcs.add(
            encode_varint(indexes[arc.source])
            + encode_varint(len(role))
            + role
            + target
            + encode_varint(len(arc.payload))
            + arc.payload
        )
    members = sorted(encoded_arcs)
    return (
        _GRAPH_DOMAIN
        + encode_varint(len(order))
        + encode_varint(len(members))
        + b"".join(encode_varint(len(member)) + member for member in members)
    )


def _bindings_for_order(
    order: tuple[str, ...],
    document_scope: bytes,
    canonical_graph: bytes,
    occurrence_ordinal: int,
) -> tuple[BlankNodeBinding, ...]:
    component_class = hashlib.sha256(
        _COMPONENT_CLASS_DOMAIN + encode_varint(len(canonical_graph)) + canonical_graph
    ).digest()
    return tuple(
        BlankNodeBinding(
            source_label=label,
            canonical_index=index,
            individual=AnonymousIndividual(
                document_scope,
                hashlib.sha256(
                    _KEY_DOMAIN
                    + document_scope
                    + component_class
                    + encode_varint(occurrence_ordinal)
                    + encode_varint(index)
                ).digest(),
            ),
        )
        for index, label in enumerate(order)
    )


def re_scope_anonymous(
    individual: AnonymousIndividual,
    new_scope: bytes,
) -> tuple[AnonymousIndividual, RescopeRecord]:
    if not isinstance(individual, AnonymousIndividual):
        raise TypeError("individual must be AnonymousIndividual")
    if not isinstance(new_scope, bytes) or len(new_scope) != 32:
        raise StructuralConstraintError("new_scope must be exactly 32 bytes")
    new_key = hashlib.sha256(
        _KEY_DOMAIN + new_scope + individual.document_scope + individual.local_key
    ).digest()
    moved = AnonymousIndividual(new_scope, new_key)
    return moved, RescopeRecord(
        old_scope=individual.document_scope,
        new_scope=new_scope,
        old_local_key=individual.local_key,
        new_local_key=new_key,
    )


__all__ = [
    "AlphaCanonicalization",
    "BlankNodeArc",
    "BlankNodeBinding",
    "alpha_canonicalize_blank_nodes",
    "canonical_document_scope",
    "re_scope_anonymous",
]
