"""Exhaustive registry-driven visitor and structural walking utilities."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Generic, TypeVar, cast

from pyowl_core.exceptions import StructuralConstraintError

from .base import CanonicalSet, StructuralNode
from .primitives import Entity, EntityKind
from .registry import MODEL_CONSTRUCTORS, constructor_spec

R = TypeVar("R")


class UnknownNodeError(StructuralConstraintError):
    """Raised when exhaustive dispatch sees an unhandled constructor."""

    DEFAULT_CODE = "UNKNOWN_MODEL_NODE"


def _method_name(value: StructuralNode) -> str:
    name = constructor_spec(value).constructor.__name__
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return "visit_" + re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).lower()


class NodeVisitor(Generic[R]):
    """Subclass and implement exact ``visit_*`` methods or override ``default``."""

    __slots__ = ()

    def visit(self, value: StructuralNode) -> R:
        if not isinstance(value, StructuralNode):
            raise TypeError("value must be a StructuralNode")
        method = getattr(self, _method_name(value), None)
        if method is None:
            return self.default(value)
        return cast(Callable[[StructuralNode], R], method)(value)

    def default(self, value: StructuralNode) -> R:
        raise UnknownNodeError(f"visitor does not handle {type(value).__name__}")


def visit_node(
    value: StructuralNode,
    handlers: Mapping[type[StructuralNode], Callable[[StructuralNode], R]],
    *,
    default: Callable[[StructuralNode], R] | None = None,
) -> R:
    if not isinstance(value, StructuralNode):
        raise TypeError("value must be a StructuralNode")
    constructor = Entity if isinstance(value, Entity) else type(value)
    handler = handlers.get(constructor)
    if handler is not None:
        return handler(value)
    if default is not None:
        return default(value)
    raise UnknownNodeError(f"no handler for {constructor.__name__}")


def iter_children(value: StructuralNode) -> Iterator[StructuralNode]:
    for field in constructor_spec(value).fields:
        child = getattr(value, field)
        if isinstance(child, StructuralNode):
            yield child
        elif isinstance(child, (CanonicalSet, tuple)):
            for item in child:
                if isinstance(item, StructuralNode):
                    yield item


def walk(value: StructuralNode) -> Iterator[StructuralNode]:
    if not isinstance(value, StructuralNode):
        raise TypeError("value must be a StructuralNode")
    stack: list[tuple[StructuralNode, bool]] = [(value, False)]
    active: set[int] = set()
    while stack:
        current, exiting = stack.pop()
        identity = id(current)
        if exiting:
            active.remove(identity)
            continue
        if identity in active:
            raise StructuralConstraintError("cyclic structural value graph")
        active.add(identity)
        yield current
        children = tuple(iter_children(current))
        stack.append((current, True))
        stack.extend((child, False) for child in reversed(children))


def _collect_signature(values: Iterable[StructuralNode]) -> tuple[Entity, ...]:
    """Collect a canonical signature without structurally hashing every occurrence."""

    entities: dict[tuple[EntityKind, str], Entity] = {}
    for value in values:
        for node in walk(value):
            if isinstance(node, Entity):
                entities[(node.kind, node.iri.value)] = node
    encoded = {node.canonical_bytes(): node for node in entities.values()}
    return tuple(encoded[key] for key in sorted(encoded))


def signature(value: StructuralNode) -> tuple[Entity, ...]:
    return _collect_signature((value,))


VISITOR_METHODS = tuple(
    "visit_"
    + re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1_\2",
        re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", constructor.__name__),
    ).lower()
    for constructor in MODEL_CONSTRUCTORS
)


__all__ = [
    "VISITOR_METHODS",
    "NodeVisitor",
    "UnknownNodeError",
    "iter_children",
    "signature",
    "visit_node",
    "walk",
]
