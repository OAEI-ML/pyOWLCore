from __future__ import annotations

import gc
import mmap
import platform
from pathlib import Path

import pytest

import pyowl_core
from pyowl_core.exceptions import SnapshotInUseError, WireCorruptionError, WireLimitError
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.encoded_views._support import (
    complete_constructor_snapshot,
    scalar_root_bytes,
)
from tools.wire_reference import encode_sections, read_wire


def test_mapped_column_validation_releases_temporary_buffer_exports(tmp_path: Path) -> None:
    path = tmp_path / "validated-columns.pyocore"
    path.write_bytes(pyowl_core.encode_snapshot(complete_constructor_snapshot()))

    mapped = pyowl_core.open_snapshot(path)
    assert isinstance(mapped, pyowl_core.MappedOntologySnapshot)

    mapped.close()

    assert mapped.closed


def test_mapped_closure_borrows_one_exporter_without_scalar_materialization(
    tmp_path: Path,
) -> None:
    source = complete_constructor_snapshot()
    expected = scalar_root_bytes(source)
    path = tmp_path / "encoded-columns.pyocore"
    path.write_bytes(pyowl_core.encode_snapshot(source))
    mapped = pyowl_core.open_snapshot(path)
    assert isinstance(mapped, pyowl_core.MappedOntologySnapshot)
    assert mapped._mapped_state.decoded is None

    encoded = mapped.view(pyowl_core.EncodedStructuralView)

    assert encoded.owner is mapped
    assert encoded.segments[0].owner is mapped
    assert decode_root_canonical_bytes(encoded.buffers) == expected
    assert mapped._mapped_state.decoded is None
    exporters = tuple(value.obj for value in encoded.buffers.values())
    assert exporters
    if platform.python_implementation() == "PyPy":
        # PyPy does not expose mmap as the direct memoryview exporter. The
        # validation boundary therefore uses the immutable-copy fallback.
        assert all(type(value) is bytes for value in exporters)
    else:
        assert type(exporters[0]) is mmap.mmap
        assert all(value is exporters[0] for value in exporters)
    assert all(value.readonly for value in encoded.buffers.values())

    with pytest.raises(SnapshotInUseError):
        mapped.close()
    del encoded
    gc.collect()
    mapped.close()
    assert mapped.closed


def test_legacy_wire_without_columns_falls_back_to_scalar_materialization(
    tmp_path: Path,
) -> None:
    source = complete_constructor_snapshot()
    current = read_wire(pyowl_core.encode_snapshot(source))
    sections = dict(current.sections)
    sections.pop(int(pyowl_core.SectionKind.ENCODED_STRUCTURAL_V1))
    legacy = encode_sections(sections, feature_flags=current.feature_flags, minor=0)
    path = tmp_path / "legacy.pyocore"
    path.write_bytes(legacy)
    mapped = pyowl_core.open_snapshot(path)
    assert isinstance(mapped, pyowl_core.MappedOntologySnapshot)
    assert mapped._mapped_state.decoded is None

    encoded = mapped.view(pyowl_core.EncodedStructuralView)

    assert decode_root_canonical_bytes(encoded.buffers) == scalar_root_bytes(source)
    assert mapped._mapped_state.decoded is not None
    assert all(type(value.obj) is bytes for value in encoded.buffers.values())
    mapped.close()


def test_mapped_columns_are_bound_to_required_view_roots(tmp_path: Path) -> None:
    first = read_wire(pyowl_core.encode_snapshot(complete_constructor_snapshot()))
    replacement_source = pyowl_core.load_snapshot(
        b"Ontology(<urn:replacement> Declaration(Class(<urn:replacement#Only>)))"
    )
    second = read_wire(pyowl_core.encode_snapshot(replacement_source))
    sections = dict(first.sections)
    sections[int(pyowl_core.SectionKind.ENCODED_STRUCTURAL_V1)] = second.sections[
        int(pyowl_core.SectionKind.ENCODED_STRUCTURAL_V1)
    ]
    hostile = encode_sections(
        sections,
        feature_flags=first.feature_flags,
        minor=first.minor,
    )
    path = tmp_path / "mismatched-columns.pyocore"
    path.write_bytes(hostile)

    with pytest.raises(WireCorruptionError):
        pyowl_core.open_snapshot(path)


def test_mapped_column_limits_fail_before_publication(tmp_path: Path) -> None:
    path = tmp_path / "limited-columns.pyocore"
    path.write_bytes(pyowl_core.encode_snapshot(complete_constructor_snapshot()))

    with pytest.raises(WireLimitError):
        pyowl_core.open_snapshot(path, limits=pyowl_core.ParseLimits(max_index_bytes=1))
