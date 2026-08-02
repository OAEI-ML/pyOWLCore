"""Language-neutral canonical model encoding and stable structural digests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from pyowl_core.exceptions import ModelError, ResourceLimitError, StructuralConstraintError

from ._tags import SCHEMA_VERSION
from .base import CanonicalSet, StructuralNode
from .primitives import (
    AnnotationProperty,
    Class,
    DataProperty,
    Datatype,
    Entity,
    EntityKind,
    NamedIndividual,
    ObjectProperty,
)
from .registry import SPEC_BY_TAG, ConstructorSpec, constructor_spec

_DOMAIN = b"pyowl-core:structural-value:v1\x00"
_NONE = 0
_NODE = 1
_TEXT = 2
_BYTES = 3
_INTEGER = 4
_ENUM = 5
_SET = 6
_SEQUENCE = 7


@dataclass(slots=True)
class _Budget:
    max_depth: int
    max_terms: int
    max_sequence_arity: int
    terms: int = 0

    @classmethod
    def from_limits(cls, limits: object | None) -> _Budget:
        return cls(
            max_depth=_limit(limits, "max_nesting_depth", 512),
            max_terms=_limit(limits, "max_terms", 500_000_000),
            max_sequence_arity=_limit(limits, "max_sequence_arity", 10_000_000),
        )

    def enter(self, depth: int) -> None:
        self.terms += 1
        if depth > self.max_depth:
            raise ResourceLimitError(
                "resource limit max_nesting_depth exceeded",
                limit="max_nesting_depth",
                observed=depth,
                allowed=self.max_depth,
            )
        if self.terms > self.max_terms:
            raise ResourceLimitError(
                "resource limit max_terms exceeded",
                limit="max_terms",
                observed=self.terms,
                allowed=self.max_terms,
            )

    def collection(self, size: int) -> None:
        if size > self.max_sequence_arity:
            raise ResourceLimitError(
                "resource limit max_sequence_arity exceeded",
                limit="max_sequence_arity",
                observed=size,
                allowed=self.max_sequence_arity,
            )


def _limit(limits: object | None, name: str, default: int) -> int:
    value = default if limits is None else getattr(limits, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _enforce_canonical_row_size(size: int, limits: object | None) -> None:
    maximum = _limit(limits, "max_canonical_work", 1_000_000_000)
    if size > maximum:
        raise ResourceLimitError(
            "resource limit max_canonical_work exceeded",
            limit="max_canonical_work",
            observed=size,
            allowed=maximum,
        )


def encode_varint(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("varint value must be an integer")
    if value < 0:
        raise StructuralConstraintError("canonical integers must be nonnegative")
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        encoded.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(encoded)


def _framed(payload: bytes) -> bytes:
    return encode_varint(len(payload)) + payload


def canonical_bytes(value: StructuralNode, *, limits: object | None = None) -> bytes:
    if not isinstance(value, StructuralNode):
        raise TypeError("value must be a StructuralNode")
    encoded = _encode_node(value, _Budget.from_limits(limits), 0, set())
    _enforce_canonical_row_size(len(encoded), limits)
    return encoded


def _encode_node(
    value: StructuralNode,
    budget: _Budget,
    depth: int,
    active: set[int],
) -> bytes:
    budget.enter(depth)
    identity = id(value)
    if identity in active:
        raise StructuralConstraintError("cyclic structural value graph")
    active.add(identity)
    try:
        spec = constructor_spec(value)
        output = bytearray(encode_varint(spec.tag))
        for field in spec.fields:
            component = getattr(value, field)
            child_depth = depth + 1
            if isinstance(component, StructuralNode):
                encoded = _encode_node(component, budget, child_depth, active)
                output.extend(bytes((_NODE,)) + _framed(encoded))
            elif isinstance(component, CanonicalSet):
                budget.collection(len(component))
                members: list[bytes] = []
                for item in component:
                    members.append(_encode_node(item, budget, child_depth, active))
                members.sort()
                output.extend(bytes((_SET,)) + encode_varint(len(members)))
                for member in members:
                    output.extend(_framed(member))
            elif isinstance(component, tuple):
                budget.collection(len(component))
                output.extend(bytes((_SEQUENCE,)) + encode_varint(len(component)))
                for item in component:
                    if isinstance(item, StructuralNode):
                        encoded = _encode_node(item, budget, child_depth, active)
                        output.extend(bytes((_NODE,)) + _framed(encoded))
                    else:
                        output.extend(_encode_scalar_component(item))
            else:
                output.extend(_encode_scalar_component(component))
        return bytes(output)
    finally:
        active.remove(identity)


def _encode_scalar_component(value: object) -> bytes:
    if value is None:
        return bytes((_NONE,))
    if isinstance(value, Enum):
        if not isinstance(value.value, str):
            raise TypeError("canonical enum values must be strings")
        return bytes((_ENUM,)) + _framed(value.value.encode("ascii"))
    if isinstance(value, str):
        return bytes((_TEXT,)) + _framed(value.encode("utf-8"))
    if isinstance(value, bytes):
        return bytes((_BYTES,)) + _framed(value)
    if isinstance(value, bool):
        raise TypeError("booleans are not canonical model integers")
    if isinstance(value, int):
        return bytes((_INTEGER,)) + encode_varint(value)
    raise TypeError(f"unsupported canonical field value: {type(value).__name__}")


def structural_digest(value: StructuralNode, *, limits: object | None = None) -> bytes:
    encoded = canonical_bytes(value, limits=limits)
    return hashlib.sha256(_DOMAIN + encode_varint(SCHEMA_VERSION) + encoded).digest()


def structural_hexdigest(value: StructuralNode, *, limits: object | None = None) -> str:
    return structural_digest(value, limits=limits).hex()


def decode_canonical(data: bytes, *, limits: object | None = None) -> StructuralNode:
    if not isinstance(data, bytes):
        raise TypeError("canonical data must be bytes")
    _enforce_canonical_row_size(len(data), limits)
    budget = _Budget.from_limits(limits)
    value, offset = _decode_node(data, 0, budget, 0)
    if offset != len(data):
        raise StructuralConstraintError("trailing bytes after canonical model value")
    if canonical_bytes(value, limits=limits) != data:
        raise StructuralConstraintError("noncanonical structural encoding")
    return value


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    start = offset
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if data[start:offset] != encode_varint(value):
                raise StructuralConstraintError("nonminimal canonical varint")
            return value, offset
        shift += 7
        if shift > 1_000_000:
            raise StructuralConstraintError("canonical varint is unreasonably long")
    raise StructuralConstraintError("truncated canonical varint")


def _take_framed(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = _decode_varint(data, offset)
    end = offset + length
    if end < offset or end > len(data):
        raise StructuralConstraintError("truncated canonical framed value")
    return data[offset:end], end


def _decode_node(
    data: bytes,
    offset: int,
    budget: _Budget,
    depth: int,
) -> tuple[StructuralNode, int]:
    budget.enter(depth)
    tag, offset = _decode_varint(data, offset)
    try:
        spec = SPEC_BY_TAG[tag]
    except KeyError as error:
        raise StructuralConstraintError(f"unknown model schema tag: {tag}") from error
    values: list[object] = []
    for _field in spec.fields:
        if offset >= len(data):
            raise StructuralConstraintError("truncated canonical component")
        marker = data[offset]
        offset += 1
        child_depth = depth + 1
        if marker == _NODE:
            payload, offset = _take_framed(data, offset)
            node_value, consumed = _decode_node(payload, 0, budget, child_depth)
            if consumed != len(payload):
                raise StructuralConstraintError("trailing bytes in nested canonical node")
            values.append(node_value)
        elif marker == _SET:
            size, offset = _decode_varint(data, offset)
            budget.collection(size)
            members: list[StructuralNode] = []
            for _ in range(size):
                payload, offset = _take_framed(data, offset)
                member, consumed = _decode_node(payload, 0, budget, child_depth)
                if consumed != len(payload):
                    raise StructuralConstraintError("trailing bytes in canonical set member")
                members.append(member)
            values.append(CanonicalSet(members))
        elif marker == _SEQUENCE:
            size, offset = _decode_varint(data, offset)
            budget.collection(size)
            sequence: list[object] = []
            for _ in range(size):
                if offset >= len(data):
                    raise StructuralConstraintError("truncated canonical sequence component")
                item_marker = data[offset]
                offset += 1
                if item_marker == _NODE:
                    payload, offset = _take_framed(data, offset)
                    node_item, consumed = _decode_node(payload, 0, budget, child_depth)
                    if consumed != len(payload):
                        raise StructuralConstraintError("trailing bytes in canonical sequence node")
                    sequence.append(node_item)
                else:
                    scalar_item, offset = _decode_scalar_component(item_marker, data, offset)
                    sequence.append(scalar_item)
            values.append(tuple(sequence))
        else:
            scalar_value, offset = _decode_scalar_component(marker, data, offset)
            values.append(scalar_value)
    return _construct(spec, values), offset


def _decode_scalar_component(
    marker: int,
    data: bytes,
    offset: int,
) -> tuple[object, int]:
    if marker == _NONE:
        return None, offset
    if marker in {_TEXT, _BYTES, _ENUM}:
        payload, offset = _take_framed(data, offset)
        if marker == _BYTES:
            return payload, offset
        try:
            return payload.decode("ascii" if marker == _ENUM else "utf-8"), offset
        except UnicodeError as error:
            raise StructuralConstraintError("invalid canonical text encoding") from error
    if marker == _INTEGER:
        return _decode_varint(data, offset)
    raise StructuralConstraintError(f"unknown canonical component marker: {marker}")


def _construct(spec: ConstructorSpec, values: list[object]) -> StructuralNode:
    try:
        if spec.constructor is Entity:
            kind = EntityKind(values[0])
            iri = values[1]
            constructors = {
                EntityKind.CLASS: Class,
                EntityKind.DATATYPE: Datatype,
                EntityKind.OBJECT_PROPERTY: ObjectProperty,
                EntityKind.DATA_PROPERTY: DataProperty,
                EntityKind.ANNOTATION_PROPERTY: AnnotationProperty,
                EntityKind.NAMED_INDIVIDUAL: NamedIndividual,
            }
            return constructors[kind](iri)  # type: ignore[arg-type]
        return spec.constructor(*values)
    except (TypeError, ValueError, ModelError) as error:
        raise StructuralConstraintError(
            f"invalid canonical {spec.constructor.__name__} payload"
        ) from error


__all__ = [
    "canonical_bytes",
    "decode_canonical",
    "encode_varint",
    "structural_digest",
    "structural_hexdigest",
]
