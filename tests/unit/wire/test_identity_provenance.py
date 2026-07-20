from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import pytest

from pyowl_core import (
    Diagnostic,
    MappedOntologySnapshot,
    OntologyDelta,
    OntologyIdentityIndex,
    SectionKind,
    Severity,
    WireCorruptionError,
    WireVersionError,
    apply_delta,
    compose_views,
    decode_snapshot,
    encode_snapshot,
    open_snapshot,
)
from tools.wire_reference import encode_sections, read_wire

from .conftest import snapshot


def test_identity_and_digests_match_direct_decoded_and_unmaterialized_mmap(
    tmp_path: Path,
) -> None:
    first = replace(
        snapshot("A"),
        diagnostics=(
            Diagnostic(
                "IDENTITY_TEST_WARNING",
                Severity.WARNING,
                "stable loader diagnostic",
                details={"count": 1},
            ),
        ),
    )
    second = snapshot("B")
    values = (
        first,
        apply_delta(first, OntologyDelta()),
        compose_views(first, second, roles=("first", "second")),
    )
    for index, value in enumerate(values):
        direct = value.view(OntologyIdentityIndex)
        encoded = encode_snapshot(value)
        assert struct.unpack_from("<H", encoded, 10)[0] == 1
        assert int(SectionKind.VIEW_PROVENANCE) in read_wire(encoded).sections

        decoded = decode_snapshot(encoded)
        assert decoded.view(OntologyIdentityIndex).documents == direct.documents
        assert (
            decoded.view(OntologyIdentityIndex).import_manifest_digest
            == direct.import_manifest_digest
        )
        assert (
            decoded.view(OntologyIdentityIndex).loader_diagnostics_digest
            == direct.loader_diagnostics_digest
        )
        assert {"wire-v1", "wire-verified"} <= decoded.capabilities.features
        assert "wire-verified" not in value.capabilities.features
        assert encode_snapshot(decoded) == encoded

        path = tmp_path / f"identity-{index}.pyocore"
        path.write_bytes(encoded)
        mapped = open_snapshot(path)
        assert isinstance(mapped, MappedOntologySnapshot)
        assert mapped._mapped_state.decoded is None
        mapped_identity = mapped.view(OntologyIdentityIndex)
        assert mapped_identity.documents == direct.documents
        assert mapped_identity.import_manifest_digest == direct.import_manifest_digest
        assert mapped_identity.loader_diagnostics_digest == direct.loader_diagnostics_digest
        assert mapped_identity.is_complete is direct.is_complete
        assert mapped._mapped_state.decoded is None
        assert {"wire-v1", "wire-verified", "mmap-snapshot"} <= mapped.capabilities.features
        mapped.close()


def test_minor_zero_manifest_metadata_has_zero_copy_identity_fallback(tmp_path: Path) -> None:
    source = snapshot("A")
    direct = source.view(OntologyIdentityIndex)
    current = read_wire(encode_snapshot(source))
    sections = dict(current.sections)
    sections.pop(int(SectionKind.ENCODED_STRUCTURAL_V1))
    encoded = encode_sections(sections, feature_flags=current.feature_flags, minor=0)
    image = read_wire(encoded)
    assert image.minor == 0
    assert int(SectionKind.VIEW_PROVENANCE) not in image.sections
    assert decode_snapshot(encoded).view(OntologyIdentityIndex).documents == direct.documents

    path = tmp_path / "minor-zero.pyocore"
    path.write_bytes(encoded)
    mapped = open_snapshot(path)
    assert mapped.view(OntologyIdentityIndex).documents == direct.documents
    assert (
        mapped.view(OntologyIdentityIndex).import_manifest_digest == direct.import_manifest_digest
    )
    assert mapped._mapped_state.decoded is None
    mapped.close()


def test_view_provenance_requires_minor_one_and_valid_bounded_rows() -> None:
    source = replace(
        snapshot("A"),
        diagnostics=(Diagnostic("IDENTITY_TEST_INFO", Severity.INFO, "test"),),
    )
    image = read_wire(encode_snapshot(source))
    sections = dict(image.sections)

    with pytest.raises(WireVersionError):
        decode_snapshot(encode_sections(sections, feature_flags=image.feature_flags, minor=0))

    provenance = bytearray(sections[int(SectionKind.VIEW_PROVENANCE)])
    struct.pack_into("<Q", provenance, 24 + 64, 0)
    sections[int(SectionKind.VIEW_PROVENANCE)] = bytes(provenance)
    with pytest.raises(WireCorruptionError):
        decode_snapshot(encode_sections(sections, feature_flags=image.feature_flags, minor=1))
