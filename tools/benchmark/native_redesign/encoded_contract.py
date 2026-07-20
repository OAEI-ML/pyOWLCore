"""Streaming common-contract evidence from encoded structural columns.

This module deliberately consumes the frozen public column schema rather than
the scalar ontology facade.  It hashes canonical records directly from the
read-only buffers and keeps only one packed native-size length table plus an
active-node bitmap.  No ``StructuralNode`` is decoded or constructed.
"""

from __future__ import annotations

import hashlib
from array import array
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from pyowl_core import AxiomScope, EncodedStructuralView, OntologySnapshot
from pyowl_core.model import CONSTRUCTOR_SPECS, encode_varint

_BUFFER_WIDTHS = {
    "root_kinds": 1,
    "root_ids": 4,
    "node_tags": 2,
    "node_field_offsets": 8,
    "field_kinds": 1,
    "field_values": 8,
    "field_lengths": 8,
    "item_kinds": 1,
    "item_values": 8,
    "item_lengths": 8,
    "scalar_bytes": 1,
}

_ROOT_ONTOLOGY_ANNOTATION = 1
_ROOT_AXIOM = 2
_ROOT_EXTENSION = 3

_SEGMENT_DIRECT = 1
_POSTINGS_ALL = 0

_NONE = 0
_NODE = 1
_TEXT = 2
_BYTES = 3
_INTEGER = 4
_ENUM = 5
_SET = 6
_SEQUENCE = 7

_AXIOM_CATEGORIES = frozenset({"annotation_axiom", "declaration_axiom", "logical_axiom"})
_SPECS_BY_TAG = {spec.tag: spec for spec in CONSTRUCTOR_SPECS}
_RECORD_INVENTORY_DOMAIN = b"pyowl-core:comparator-record-inventory:v1\x00"


class EncodedContractUnavailable(RuntimeError):
    """The selected view cannot truthfully satisfy the bulk timing fence."""


class EncodedContractError(ValueError):
    """The encoded view is malformed or disagrees with the frozen schema."""


class _Sink(Protocol):
    def update(self, value: bytes | bytearray | memoryview) -> None: ...


@dataclass(frozen=True, slots=True)
class DigestResult:
    """One complete streamed preimage digest and byte count."""

    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class EncodedTraversalEvidence:
    """Bounded proof facts for the installed native comparator lane."""

    view_count: int
    document_view_count: int
    node_count: int
    root_count: int
    referenced_buffer_bytes: int
    referenced_buffer_copy_bytes: int = 0
    scalar_traversal_calls: int = 0
    structural_nodes_materialized: int = 0

    def to_metrics(self) -> dict[str, int]:
        return {
            "encoded_view_count": self.view_count,
            "encoded_document_view_count": self.document_view_count,
            "encoded_node_count": self.node_count,
            "encoded_root_count": self.root_count,
            "encoded_referenced_buffer_bytes": self.referenced_buffer_bytes,
            "encoded_referenced_buffer_copy_bytes": self.referenced_buffer_copy_bytes,
            "encoded_scalar_traversal_calls": self.scalar_traversal_calls,
            "encoded_structural_nodes_materialized": self.structural_nodes_materialized,
        }


class _DigestSink:
    __slots__ = ("_digest", "byte_count")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.byte_count = 0

    def update(self, value: bytes | bytearray | memoryview) -> None:
        self._digest.update(value)
        self.byte_count += len(value)

    def finish(self) -> DigestResult:
        return DigestResult(self._digest.hexdigest(), self.byte_count)


class _Column:
    __slots__ = ("data", "name", "width")

    def __init__(self, data: memoryview, width: int, name: str) -> None:
        if len(data) % width:
            raise EncodedContractError(f"encoded {name} column is misaligned")
        self.data = data
        self.width = width
        self.name = name

    def __len__(self) -> int:
        return len(self.data) // self.width

    def __getitem__(self, index: int) -> int:
        if index < 0 or index >= len(self):
            raise EncodedContractError(f"encoded {self.name} row is out of range")
        start = index * self.width
        return int.from_bytes(self.data[start : start + self.width], "little")


class EncodedStructuralTraversal:
    """Validated streaming access to one direct structural-column view."""

    __slots__ = (
        "_active",
        "_buffers",
        "_field_kinds",
        "_field_lengths",
        "_field_offsets",
        "_field_values",
        "_item_kinds",
        "_item_lengths",
        "_item_values",
        "_node_lengths",
        "_root_ids",
        "_root_kinds",
        "_scalars",
        "_tags",
        "view",
    )

    def __init__(
        self,
        view: object,
        *,
        owner: OntologySnapshot,
        scope: AxiomScope,
        document_key: str | None,
        require_native_direct: bool,
    ) -> None:
        if type(view) is not EncodedStructuralView:
            raise EncodedContractError("encoded traversal requires the exact schema-v1 view")
        selected = view
        if selected.owner is not owner:
            raise EncodedContractError("encoded traversal lost its exact snapshot owner")
        if selected.scope is not scope or selected.document_key != document_key:
            raise EncodedContractError("encoded traversal selection metadata differs")
        if selected.schema_name != EncodedStructuralView.SCHEMA_NAME:
            raise EncodedContractError("encoded traversal schema name differs")
        if selected.schema_version != EncodedStructuralView.SCHEMA_VERSION:
            raise EncodedContractError("encoded traversal schema version differs")
        if selected.model_schema != 1:
            raise EncodedContractError("encoded traversal model schema differs")
        if hashlib.sha256(selected.descriptor).digest() != EncodedStructuralView.DESCRIPTOR_SHA256:
            raise EncodedContractError("encoded traversal descriptor digest differs")
        self._validate_direct_segments(selected, owner)
        self._buffers = self._validate_buffers(selected.buffers, require_native_direct)
        self.view = selected

        self._root_kinds = cast(_Column, self._buffers["root_kinds"])
        self._root_ids = cast(_Column, self._buffers["root_ids"])
        self._tags = cast(_Column, self._buffers["node_tags"])
        self._field_offsets = cast(_Column, self._buffers["node_field_offsets"])
        self._field_kinds = cast(_Column, self._buffers["field_kinds"])
        self._field_values = cast(_Column, self._buffers["field_values"])
        self._field_lengths = cast(_Column, self._buffers["field_lengths"])
        self._item_kinds = cast(_Column, self._buffers["item_kinds"])
        self._item_values = cast(_Column, self._buffers["item_values"])
        self._item_lengths = cast(_Column, self._buffers["item_lengths"])
        self._scalars = cast(memoryview, self._buffers["scalar_bytes"])
        self._node_lengths = array("Q", [0]) * (len(self._tags) + 1)
        self._active = bytearray(len(self._tags) + 1)
        self._validate_shape()

    @classmethod
    def from_snapshot(
        cls,
        snapshot: OntologySnapshot,
        *,
        scope: AxiomScope,
        document_key: str | None = None,
        require_native_direct: bool = True,
    ) -> EncodedStructuralTraversal:
        options: dict[str, object] = {"scope": scope}
        if document_key is not None:
            options["document_key"] = document_key
        view = snapshot.view(EncodedStructuralView, **options)
        return cls(
            view,
            owner=snapshot,
            scope=scope,
            document_key=document_key,
            require_native_direct=require_native_direct,
        )

    @property
    def node_count(self) -> int:
        return len(self._tags)

    @property
    def root_count(self) -> int:
        return len(self._root_ids)

    @property
    def referenced_buffer_bytes(self) -> int:
        return sum(len(value) for value in self.view.buffers.values())

    def record_inventory(self, root_kind: int) -> dict[str, object]:
        count = self._root_count(root_kind)
        canonical_size = sum(self._node_length(node_id) for node_id in self._roots(root_kind))
        sink = _DigestSink()
        sink.update(_RECORD_INVENTORY_DOMAIN)
        sink.update(encode_varint(count))
        self.write_collection_records(sink, root_kind)
        result = sink.finish()
        return {
            "count": count,
            "canonical_bytes": canonical_size,
            "transcript_bytes": result.byte_count,
            "sha256": result.sha256,
        }

    def document_preimage(
        self,
        *,
        ontology_iri: bytes | None,
        version_iri: bytes | None,
        direct_imports: Sequence[bytes],
    ) -> DigestResult:
        sink = _DigestSink()
        sink.update(b"pyowl-core:document-fingerprint:v1\x00")
        for iri in (ontology_iri, version_iri):
            if iri is None:
                sink.update(b"0")
            else:
                sink.update(b"1")
                _write_frame(sink, iri)
        _write_byte_collection(sink, direct_imports)
        for root_kind in (
            _ROOT_ONTOLOGY_ANNOTATION,
            _ROOT_AXIOM,
            _ROOT_EXTENSION,
        ):
            self.write_collection(sink, root_kind)
        return sink.finish()

    @staticmethod
    def structural_preimage(
        manifest_bytes: bytes,
        documents: Sequence[tuple[str, EncodedStructuralTraversal]],
    ) -> DigestResult:
        sink = _DigestSink()
        sink.update(b"pyowl-core:snapshot-structural:v1\x00")
        _write_frame(sink, manifest_bytes)
        for document_key, traversal in documents:
            _write_frame(sink, document_key.encode("ascii"))
            for root_kind in (
                _ROOT_ONTOLOGY_ANNOTATION,
                _ROOT_AXIOM,
                _ROOT_EXTENSION,
            ):
                traversal.write_collection(sink, root_kind)
        return sink.finish()

    def logical_preimage(self) -> DigestResult:
        logical_count = 0
        for node_id in self._roots(_ROOT_AXIOM):
            spec = _SPECS_BY_TAG[self._tag(node_id)]
            if spec.category == "logical_axiom":
                self._require_empty_top_annotations(node_id)
                logical_count += 1
        extension_count = self._root_count(_ROOT_EXTENSION)
        for node_id in self._roots(_ROOT_EXTENSION):
            self._require_empty_top_annotations(node_id)

        sink = _DigestSink()
        sink.update(b"pyowl-core:snapshot-logical:v1\x00")
        sink.update(b"datatype-policy:owl2-v1\x00")
        sink.update(encode_varint(logical_count))
        for node_id in self._roots(_ROOT_AXIOM):
            if _SPECS_BY_TAG[self._tag(node_id)].category == "logical_axiom":
                self._write_framed_node(sink, node_id)
        sink.update(encode_varint(extension_count))
        for node_id in self._roots(_ROOT_EXTENSION):
            sink.update(b"E")
            self._write_framed_node(sink, node_id)
        return sink.finish()

    def signature_preimage(self) -> DigestResult:
        entity_count = sum(self._tag(node_id) == 2 for node_id in range(1, self.node_count + 1))
        sink = _DigestSink()
        sink.update(b"pyowl-core:snapshot-signature:v1\x00")
        sink.update(b"\x01")
        sink.update(encode_varint(entity_count))
        self._write_entity_records(sink)
        return sink.finish()

    def signature_inventory(self) -> dict[str, object]:
        entity_count = sum(self._tag(node_id) == 2 for node_id in range(1, self.node_count + 1))
        canonical_size = sum(
            self._node_length(node_id)
            for node_id in range(1, self.node_count + 1)
            if self._tag(node_id) == 2
        )
        sink = _DigestSink()
        sink.update(_RECORD_INVENTORY_DOMAIN)
        sink.update(encode_varint(entity_count))
        self._write_entity_records(sink)
        result = sink.finish()
        return {
            "count": entity_count,
            "canonical_bytes": canonical_size,
            "transcript_bytes": result.byte_count,
            "sha256": result.sha256,
        }

    def write_collection(self, sink: _Sink, root_kind: int) -> None:
        sink.update(encode_varint(self._root_count(root_kind)))
        self.write_collection_records(sink, root_kind)

    def write_collection_records(self, sink: _Sink, root_kind: int) -> None:
        for node_id in self._roots(root_kind):
            self._write_framed_node(sink, node_id)

    @staticmethod
    def _validate_direct_segments(view: EncodedStructuralView, owner: OntologySnapshot) -> None:
        if len(view.segments) != 1:
            raise EncodedContractUnavailable(
                "bulk common-contract traversal requires one direct encoded segment"
            )
        segment = view.segments[0]
        if (
            segment.role != _SEGMENT_DIRECT
            or segment.owner is not owner
            or segment.source is not None
            or segment.posting_mode != _POSTINGS_ALL
            or len(segment.root_ids)
            or len(segment.anonymous_scope_map)
            or segment.member_token is not None
        ):
            raise EncodedContractUnavailable(
                "bulk common-contract traversal requires an unfiltered direct segment"
            )

    @staticmethod
    def _validate_buffers(
        buffers: Mapping[str, memoryview], require_native_direct: bool
    ) -> dict[str, _Column | memoryview]:
        if set(buffers) != set(_BUFFER_WIDTHS):
            raise EncodedContractError("encoded buffer names differ from schema v1")
        exporters: list[object] = []
        selected: dict[str, _Column | memoryview] = {}
        for name, width in _BUFFER_WIDTHS.items():
            value = buffers[name]
            if (
                type(value) is not memoryview
                or not value.readonly
                or not value.c_contiguous
                or value.ndim != 1
                or value.itemsize != 1
                or value.format != "B"
                or value.shape != (len(value),)
                or value.strides != (1,)
            ):
                raise EncodedContractError(f"encoded {name} is not a canonical byte view")
            exporters.append(value.obj)
            selected[name] = value if name == "scalar_bytes" else _Column(value, width, name)
        if require_native_direct and (
            not exporters
            or type(exporters[0]) is not bytes
            or any(value is not exporters[0] for value in exporters[1:])
        ):
            raise EncodedContractUnavailable(
                "installed native lane did not publish one retained immutable column exporter"
            )
        return selected

    def _validate_shape(self) -> None:
        if len(self._root_kinds) != len(self._root_ids):
            raise EncodedContractError("encoded root columns have different lengths")
        if len(self._field_offsets) != self.node_count + 1:
            raise EncodedContractError("encoded field offsets do not cover every node")
        if not len(self._field_offsets) or self._field_offsets[0] != 0:
            raise EncodedContractError("encoded field offsets do not start at zero")
        if self._field_offsets[len(self._field_offsets) - 1] != len(self._field_kinds):
            raise EncodedContractError("encoded field offsets do not end at the field count")
        if not (len(self._field_kinds) == len(self._field_values) == len(self._field_lengths)):
            raise EncodedContractError("encoded field columns have different lengths")
        if not (len(self._item_kinds) == len(self._item_values) == len(self._item_lengths)):
            raise EncodedContractError("encoded item columns have different lengths")

        for node_id in range(1, self.node_count + 1):
            tag = self._tag(node_id)
            spec = _SPECS_BY_TAG.get(tag)
            if spec is None:
                raise EncodedContractError("encoded node tag is outside model schema v1")
            start, end = self._field_range(node_id)
            if end - start != len(spec.fields):
                raise EncodedContractError("encoded node field count differs from its constructor")
            self._node_length(node_id)

        for index in range(self.root_count):
            root_kind = self._root_kinds[index]
            node_id = self._root_ids[index]
            tag = self._tag(node_id)
            spec = _SPECS_BY_TAG[tag]
            if root_kind == _ROOT_ONTOLOGY_ANNOTATION and tag != 5:
                raise EncodedContractError("ontology-annotation root has the wrong constructor")
            if root_kind == _ROOT_AXIOM and spec.category not in _AXIOM_CATEGORIES:
                raise EncodedContractError("axiom root has the wrong constructor category")
            if root_kind == _ROOT_EXTENSION and tag != 148:
                raise EncodedContractError("extension root has the wrong constructor")
            if root_kind not in {
                _ROOT_ONTOLOGY_ANNOTATION,
                _ROOT_AXIOM,
                _ROOT_EXTENSION,
            }:
                raise EncodedContractError("encoded root kind is outside schema v1")

    def _roots(self, root_kind: int) -> Iterator[int]:
        for index in range(self.root_count):
            if self._root_kinds[index] == root_kind:
                yield self._root_ids[index]

    def _write_entity_records(self, sink: _Sink) -> None:
        for node_id in range(1, self.node_count + 1):
            if self._tag(node_id) == 2:
                self._write_framed_node(sink, node_id)

    def _root_count(self, root_kind: int) -> int:
        return sum(self._root_kinds[index] == root_kind for index in range(self.root_count))

    def _tag(self, node_id: int) -> int:
        if node_id <= 0 or node_id > self.node_count:
            raise EncodedContractError("encoded node ID is outside the dense table")
        return self._tags[node_id - 1]

    def _field_range(self, node_id: int) -> tuple[int, int]:
        self._tag(node_id)
        start = self._field_offsets[node_id - 1]
        end = self._field_offsets[node_id]
        if end < start or end > len(self._field_kinds):
            raise EncodedContractError("encoded node field range exceeds its columns")
        return start, end

    def _scalar_range(self, start: int, length: int) -> memoryview:
        end = start + length
        if end < start or end > len(self._scalars):
            raise EncodedContractError("encoded scalar range exceeds scalar_bytes")
        return self._scalars[start:end]

    def _node_length(self, node_id: int) -> int:
        self._tag(node_id)
        cached = self._node_lengths[node_id]
        if cached:
            return cached
        if self._active[node_id]:
            raise EncodedContractError("encoded node graph is cyclic")
        self._active[node_id] = 1
        try:
            length = len(encode_varint(self._tag(node_id)))
            start, end = self._field_range(node_id)
            for index in range(start, end):
                length += self._component_length(
                    self._field_kinds[index],
                    self._field_values[index],
                    self._field_lengths[index],
                )
            if length <= 0 or length > 2**64 - 1:
                raise EncodedContractError("encoded canonical node length exceeds u64")
            self._node_lengths[node_id] = length
            return length
        finally:
            self._active[node_id] = 0

    def _component_length(self, kind: int, value: int, length: int) -> int:
        if kind == _NONE:
            if value or length:
                raise EncodedContractError("encoded none component carries a payload")
            return 1
        if kind == _NODE:
            if length:
                raise EncodedContractError("encoded node component carries a length")
            child_length = self._node_length(value)
            return 1 + len(encode_varint(child_length)) + child_length
        if kind in {_TEXT, _BYTES, _ENUM}:
            self._scalar_range(value, length)
            return 1 + len(encode_varint(length)) + length
        if kind == _INTEGER:
            payload = self._scalar_range(value, length)
            if not payload or (len(payload) > 1 and payload[-1] == 0):
                raise EncodedContractError("encoded integer payload is not minimal")
            return 1 + len(encode_varint(int.from_bytes(payload, "little")))
        if kind not in {_SET, _SEQUENCE}:
            raise EncodedContractError("encoded component kind is outside schema v1")
        end = value + length
        if end < value or end > len(self._item_kinds):
            raise EncodedContractError("encoded item range exceeds its columns")
        total = 1 + len(encode_varint(length))
        for index in range(value, end):
            item_kind = self._item_kinds[index]
            item_value = self._item_values[index]
            item_length = self._item_lengths[index]
            if kind == _SET:
                if item_kind != _NODE or item_length:
                    raise EncodedContractError("encoded canonical-set item is not a node")
                child_length = self._node_length(item_value)
                total += len(encode_varint(child_length)) + child_length
            else:
                total += self._component_length(item_kind, item_value, item_length)
        return total

    def _write_framed_node(self, sink: _Sink, node_id: int) -> None:
        sink.update(encode_varint(self._node_length(node_id)))
        self._write_node(sink, node_id)

    def _write_node(self, sink: _Sink, node_id: int) -> None:
        sink.update(encode_varint(self._tag(node_id)))
        start, end = self._field_range(node_id)
        for index in range(start, end):
            self._write_component(
                sink,
                self._field_kinds[index],
                self._field_values[index],
                self._field_lengths[index],
            )

    def _write_component(self, sink: _Sink, kind: int, value: int, length: int) -> None:
        self._component_length(kind, value, length)
        sink.update(bytes((kind,)))
        if kind == _NONE:
            return
        if kind == _NODE:
            self._write_framed_node(sink, value)
            return
        if kind in {_TEXT, _BYTES, _ENUM}:
            sink.update(encode_varint(length))
            sink.update(self._scalar_range(value, length))
            return
        if kind == _INTEGER:
            sink.update(encode_varint(int.from_bytes(self._scalar_range(value, length), "little")))
            return
        sink.update(encode_varint(length))
        end = value + length
        for index in range(value, end):
            item_kind = self._item_kinds[index]
            item_value = self._item_values[index]
            item_length = self._item_lengths[index]
            if kind == _SET:
                self._write_framed_node(sink, item_value)
            else:
                self._write_component(sink, item_kind, item_value, item_length)

    def _require_empty_top_annotations(self, node_id: int) -> None:
        spec = _SPECS_BY_TAG[self._tag(node_id)]
        if not spec.fields or spec.fields[-1] != "annotations":
            raise EncodedContractError("logical root has no annotations field in schema v1")
        _, end = self._field_range(node_id)
        field_index = end - 1
        if self._field_kinds[field_index] != _SET:
            raise EncodedContractError("logical root annotations field is not a canonical set")
        if self._field_lengths[field_index] != 0:
            raise EncodedContractUnavailable(
                "bulk logical fingerprint normalization for annotated roots is not implemented"
            )


def combine_traversal_evidence(
    closure: EncodedStructuralTraversal,
    documents: Sequence[EncodedStructuralTraversal],
) -> EncodedTraversalEvidence:
    """Combine exact direct-view facts without counting any copied payload."""

    values = (closure, *documents)
    return EncodedTraversalEvidence(
        view_count=len(values),
        document_view_count=len(documents),
        node_count=sum(value.node_count for value in values),
        root_count=sum(value.root_count for value in values),
        referenced_buffer_bytes=sum(value.referenced_buffer_bytes for value in values),
    )


def _write_frame(sink: _Sink, value: bytes) -> None:
    sink.update(encode_varint(len(value)))
    sink.update(value)


def _write_byte_collection(sink: _Sink, values: Sequence[bytes]) -> None:
    sink.update(encode_varint(len(values)))
    for value in values:
        _write_frame(sink, value)


__all__ = [
    "DigestResult",
    "EncodedContractError",
    "EncodedContractUnavailable",
    "EncodedStructuralTraversal",
    "EncodedTraversalEvidence",
    "combine_traversal_evidence",
]
