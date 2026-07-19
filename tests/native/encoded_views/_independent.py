"""Small consumer-style decoder independent of the runtime validator."""

from __future__ import annotations

from collections.abc import Mapping


def _varint(value: int) -> bytes:
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


def _frame(value: bytes) -> bytes:
    return _varint(len(value)) + value


def _column(data: memoryview, width: int) -> tuple[int, ...]:
    return tuple(
        int.from_bytes(data[offset : offset + width], "little")
        for offset in range(0, len(data), width)
    )


def decode_root_canonical_bytes(
    buffers: Mapping[str, memoryview],
) -> tuple[tuple[int, bytes], ...]:
    """Decode only the documented columns into canonical-model-v1 root bytes."""

    root_kinds = _column(buffers["root_kinds"], 1)
    root_ids = _column(buffers["root_ids"], 4)
    tags = _column(buffers["node_tags"], 2)
    field_offsets = _column(buffers["node_field_offsets"], 8)
    field_kinds = _column(buffers["field_kinds"], 1)
    field_values = _column(buffers["field_values"], 8)
    field_lengths = _column(buffers["field_lengths"], 8)
    item_kinds = _column(buffers["item_kinds"], 1)
    item_values = _column(buffers["item_values"], 8)
    item_lengths = _column(buffers["item_lengths"], 8)
    scalars = buffers["scalar_bytes"]
    memo: dict[int, bytes] = {}

    def component(kind: int, value: int, length: int) -> bytes:
        if kind == 0:
            return b"\x00"
        if kind == 1:
            return b"\x01" + _frame(node(value))
        if kind in {2, 3, 5}:
            return bytes((kind,)) + _frame(bytes(scalars[value : value + length]))
        if kind == 4:
            integer = int.from_bytes(scalars[value : value + length], "little")
            return b"\x04" + _varint(integer)
        marker = b"\x06" if kind == 6 else b"\x07"
        output = bytearray(marker + _varint(length))
        for index in range(value, value + length):
            if kind == 6:
                output.extend(_frame(node(item_values[index])))
            else:
                output.extend(component(item_kinds[index], item_values[index], item_lengths[index]))
        return bytes(output)

    def node(node_id: int) -> bytes:
        cached = memo.get(node_id)
        if cached is not None:
            return cached
        index = node_id - 1
        output = bytearray(_varint(tags[index]))
        for field_index in range(field_offsets[index], field_offsets[index + 1]):
            output.extend(
                component(
                    field_kinds[field_index],
                    field_values[field_index],
                    field_lengths[field_index],
                )
            )
        encoded = bytes(output)
        memo[node_id] = encoded
        return encoded

    return tuple((kind, node(node_id)) for kind, node_id in zip(root_kinds, root_ids, strict=True))


__all__ = ["decode_root_canonical_bytes"]
