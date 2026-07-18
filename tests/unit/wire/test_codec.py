from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import pyowl_core.extensions.swrl as swrl
import pyowl_core.model as m
import pyowl_core.wire.codec as wire_codec
from pyowl_core import (
    BackendPreference,
    DetectionBasis,
    DigestKind,
    DocumentFormat,
    DocumentProvenance,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    OntologyDelta,
    OntologyDocument,
    OntologyID,
    ParseLimits,
    apply_delta,
    compose_views,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
)
from tests.generated.model.fixtures import model_fixtures
from tools.wire_reference import encode_sections, read_wire, reencode
from tools.wire_reference.check_schema import check_schema

from .conftest import snapshot


def test_fixed_header_required_inventory_and_independent_reencode() -> None:
    encoded = encode_snapshot(snapshot("A", "B"))
    assert encoded[:8] == b"PYOCORE\0"
    assert len(encoded) == struct.unpack_from("<Q", encoded, 32)[0]
    assert struct.unpack_from("<I", encoded, 12)[0] == 96
    reference = read_wire(encoded)
    assert {entry.kind for entry in reference.entries if entry.flags == 1} == set(range(1, 15))
    assert reencode(reference) == encoded
    root = Path(__file__).resolve().parents[3]
    assert check_schema(root / "schemas" / "wire-v1.toml") == ()


def test_empty_v1_bytes_match_frozen_golden_digest() -> None:
    golden_path = Path(__file__).with_name("goldens") / "empty-v1.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    encoded = encode_snapshot(snapshot())
    assert len(encoded) == golden["length"]
    assert hashlib.sha256(encoded).hexdigest() == golden["sha256"]
    assert encoded[56:88].hex() == golden["wire_digest"]


def test_bytes_buffer_stream_and_effective_views_round_trip_canonically() -> None:
    base = snapshot("A")
    other = snapshot("Z")
    added = m.Declaration(m.Class(m.IRI("urn:wire#B")))
    values = (
        base,
        apply_delta(base, OntologyDelta(add_axioms=m.CanonicalSet[m.AxiomNode]((added,)))),
        compose_views(base, other, roles=("source", "target")),
    )
    for value in values:
        encoded = encode_snapshot(value)
        for source in (encoded, bytearray(encoded), memoryview(encoded), io.BytesIO(encoded)):
            decoded = decode_snapshot(source)
            assert decoded.structural_fingerprint == value.structural_fingerprint
            assert decode_snapshot(encode_snapshot(decoded)) == decoded
            assert encode_snapshot(decoded) == encoded


def test_eager_decode_reuses_rows_materialized_during_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = encode_snapshot(snapshot("A", "B"))
    observed: list[bytes] = []
    original = wire_codec._decode_model

    def counting_decode(row: memoryview, limits: ParseLimits) -> m.StructuralNode:
        observed.append(bytes(row))
        return original(row, limits)

    monkeypatch.setattr(wire_codec, "_decode_model", counting_decode)
    decoded = decode_snapshot(encoded)

    assert tuple(decoded.iter_axioms())
    assert observed
    assert len(observed) == len(set(observed))


def test_every_registered_constructor_and_swrl_extension_round_trip() -> None:
    fixtures = model_fixtures()
    provenance = DocumentProvenance(
        hashlib.sha256(b"wire-every-constructor").digest(),
        DigestKind.EXACT_BYTES,
        0,
        0,
        None,
        None,
        DocumentFormat.FUNCTIONAL,
        DetectionBasis.EXPLICIT,
    )
    document = OntologyDocument(
        OntologyID(),
        None,
        (),
        m.CanonicalSet((cast(m.Annotation, fixtures[m.Annotation]),)),
        m.CanonicalSet(value for value in fixtures.values() if isinstance(value, m.AxiomNode)),
        m.CanonicalSet((fixtures[swrl.SWRLRule],)),
        provenance,
    )
    source = load_snapshot(
        document,
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )
    encoded = encode_snapshot(source)
    decoded = decode_snapshot(encoded)
    assert tuple(decoded.iter_axioms()) == tuple(source.iter_axioms())
    assert tuple(decoded.iter_extensions()) == tuple(source.iter_extensions())
    assert encode_snapshot(decoded) == encoded
    image = read_wire(encoded)
    assert 0x8001 in image.sections


def test_new_minor_and_unknown_optional_section_are_skippable() -> None:
    source = snapshot("A")
    image = read_wire(encode_snapshot(source))
    newer = encode_sections(image.sections, feature_flags=image.feature_flags, minor=41)
    assert decode_snapshot(newer).structural_fingerprint == source.structural_fingerprint

    sections = dict(image.sections)
    sections[60_000] = b"opaque optional payload"
    extended = encode_sections(sections, feature_flags=image.feature_flags)
    decoded = decode_snapshot(extended)
    assert decoded.structural_fingerprint == source.structural_fingerprint
    assert encode_snapshot(decoded) == encode_snapshot(source)


def test_import_cycle_manifest_and_safe_metadata_round_trip() -> None:
    first = b"Ontology(<urn:a> Import(<urn:b>) Declaration(Class(<urn:A>)))"
    second = b"Ontology(<urn:b> Import(<urn:a>) Declaration(Class(<urn:B>)))"
    source = load_snapshot(
        first,
        options=LoadOptions(
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.PYTHON,
        ),
        resolver=MappingResolver({"urn:a": first, "urn:b": second}),
    )
    encoded = encode_snapshot(source)
    decoded = decode_snapshot(encoded)
    assert decoded.import_manifest.canonical_bytes() == source.import_manifest.canonical_bytes()
    assert decoded.import_manifest.edges == source.import_manifest.edges
    assert any(
        decoded_record.source_sha256 != source_record.source_sha256
        for decoded_record, source_record in zip(
            decoded.import_manifest.documents,
            source.import_manifest.documents,
            strict=True,
        )
    )
    assert len(decoded.documents) == 2
    assert decoded.is_complete
    secret = "file:///private/ontology?authorization=Bearer-SECRET"
    private_document = replace(
        source.root,
        provenance=replace(source.root.provenance, acquisition_locator=secret),
    )
    private_snapshot = load_snapshot(
        private_document,
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )
    private_encoded = encode_snapshot(private_snapshot)
    assert b"bearer-secret" not in private_encoded.lower()
    assert b"acquisition_locator" not in private_encoded


def test_wire_bytes_are_hash_seed_independent() -> None:
    script = """
import hashlib
from pyowl_core import BackendPreference, ImportPolicy, LoadOptions, encode_snapshot, load_snapshot
source = b'Prefix(:=<urn:wire#>) Ontology(<urn:wire> Declaration(Class(:B)) Declaration(Class(:A)))'
options = LoadOptions(
    imports=ImportPolicy.IGNORE,
    backend=BackendPreference.PYTHON,
)
snapshot = load_snapshot(source, options=options)
print(hashlib.sha256(encode_snapshot(snapshot)).hexdigest())
"""
    outputs = []
    for seed in ("1", "8675309"):
        environment = dict(os.environ)
        environment.update(PYTHONPATH="src", PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout
        )
    assert outputs[0] == outputs[1]
