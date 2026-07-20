from __future__ import annotations

import hashlib
import json
import struct
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, cast

import pyowl_core.extensions.swrl as swrl
import pyowl_core.model as model
from pyowl_core import (
    BackendPreference,
    DetectionBasis,
    Diagnostic,
    DigestKind,
    DocumentFormat,
    DocumentProvenance,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    OntologyDocument,
    OntologyID,
    ParseLimits,
    SectionKind,
    Severity,
    WireError,
    apply_delta,
    compose_views,
    encode_snapshot,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.cancellation import CancellationSource
from pyowl_core.exceptions import OperationCancelledError
from tests.generated.model.fixtures import model_fixtures
from tests.native.foundation._support import NativeTestExtension, load_extension
from tests.unit.wire.conftest import snapshot
from tools.wire_reference import encode_sections, read_wire, reencode


class NativeWireParityTests(unittest.TestCase):
    extension: ClassVar[NativeTestExtension]

    @classmethod
    def setUpClass(cls) -> None:
        cls.extension = load_extension()
        native._reset_probe_cache_for_tests()
        result = native.probe(refresh=True)
        if not result.available:
            raise unittest.SkipTest(result.reason or "native extension is unavailable")

    def test_empty_golden_and_independent_reader_are_byte_identical(self) -> None:
        encoded = encode_snapshot(snapshot())
        golden = json.loads(
            (Path(__file__).parents[2] / "unit" / "wire" / "goldens" / "empty-v1.json").read_text(
                encoding="utf-8"
            )
        )
        native_encoded = native.encode_snapshot(snapshot())
        self.assertEqual(native_encoded, encoded)
        self.assertEqual(len(encoded), golden["length"])
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), golden["sha256"])
        self.assertEqual(native.roundtrip_wire(encoded), encoded)
        self.assertEqual(reencode(read_wire(native_encoded)), encoded)

    def test_every_constructor_swrl_overlay_and_composite_have_full_parity(self) -> None:
        fixtures = model_fixtures()
        provenance = DocumentProvenance(
            hashlib.sha256(b"native-wire-every-constructor").digest(),
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
            model.CanonicalSet((cast(model.Annotation, fixtures[model.Annotation]),)),
            model.CanonicalSet(
                value for value in fixtures.values() if isinstance(value, model.AxiomNode)
            ),
            model.CanonicalSet((fixtures[swrl.SWRLRule],)),
            provenance,
        )
        base = load_snapshot(
            document,
            options=LoadOptions(
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.PYTHON,
            ),
        )
        added = model.Declaration(model.Class(model.IRI("urn:native#Added")))
        overlay = apply_delta(
            base,
            OntologyDelta(add_axioms=model.CanonicalSet[model.AxiomNode]((added,))),
        )
        composite = compose_views(base, snapshot("Other"), roles=("source", "target"))
        for value in (base, overlay, composite):
            with self.subTest(view=type(value).__name__):
                python_wire = encode_snapshot(value)
                native_wire = native.encode_snapshot(value)
                self.assertEqual(native_wire, python_wire)
                decoded = native.decode_snapshot(native_wire)
                self.assertEqual(decoded.structural_fingerprint, value.structural_fingerprint)
                self.assertEqual(encode_snapshot(decoded), python_wire)

    def test_all_truncations_and_systematic_integrity_corruptions_are_typed(self) -> None:
        encoded = encode_snapshot(snapshot())
        for length in range(len(encoded)):
            with self.subTest(length=length), self.assertRaises(WireError):
                native.roundtrip_wire(encoded[:length])
        for offset in (0, 8, 16, 55, 88, 96, 167, len(encoded) - 1):
            corrupt = bytearray(encoded)
            corrupt[offset] ^= 0x40
            with self.subTest(offset=offset), self.assertRaises(WireError):
                native.roundtrip_wire(corrupt)

    def test_resigned_semantic_corruption_is_rejected(self) -> None:
        encoded = encode_snapshot(snapshot("A"))
        image = read_wire(encoded)
        sections = dict(image.sections)
        strings = bytearray(sections[1])
        count = struct.unpack_from("<Q", strings, 0)[0]
        payload = 8 * (count + 2)
        strings[payload] = 0xFF
        sections[1] = bytes(strings)
        hostile = encode_sections(sections, feature_flags=image.feature_flags, minor=image.minor)
        with self.assertRaises(WireError):
            native.roundtrip_wire(hostile)

        sections = dict(image.sections)
        iri_table = bytearray(sections[2])
        iri_count = struct.unpack_from("<Q", iri_table, 0)[0]
        last_offset = struct.unpack_from("<Q", iri_table, 8 + (iri_count - 1) * 8)[0]
        payload = 8 * (iri_count + 2)
        iri_table[payload + last_offset + 1] = 4  # IRI text marker -> integer marker.
        sections[2] = bytes(iri_table)
        hostile = encode_sections(sections, feature_flags=image.feature_flags, minor=image.minor)
        with self.assertRaises(WireError):
            native.roundtrip_wire(hostile)

    def test_view_provenance_minor_one_is_validated_without_materialization(self) -> None:
        source = replace(
            snapshot("A"),
            diagnostics=(Diagnostic("NATIVE_IDENTITY_TEST", Severity.INFO, "test"),),
        )
        encoded = encode_snapshot(source)
        image = read_wire(encoded)
        self.assertEqual(image.minor, 1)
        self.assertEqual(native.roundtrip_wire(encoded), encoded)

        sections = dict(image.sections)
        provenance = bytearray(sections[int(SectionKind.VIEW_PROVENANCE)])
        struct.pack_into("<Q", provenance, 24 + 64, 0)
        sections[int(SectionKind.VIEW_PROVENANCE)] = bytes(provenance)
        hostile = encode_sections(
            sections,
            feature_flags=image.feature_flags,
            minor=image.minor,
        )
        with self.assertRaises(WireError):
            native.roundtrip_wire(hostile)

    def test_encoded_structural_section_framing_and_columns_are_validated(self) -> None:
        encoded = encode_snapshot(snapshot("A"))
        image = read_wire(encoded)
        kind = int(SectionKind.ENCODED_STRUCTURAL_V1)
        self.assertIn(kind, image.sections)
        self.assertEqual(native.roundtrip_wire(encoded), encoded)

        with self.assertRaises(WireError):
            native.roundtrip_wire(
                encode_sections(
                    image.sections,
                    feature_flags=image.feature_flags,
                    minor=0,
                )
            )

        sections = dict(image.sections)
        descriptor = bytearray(sections[kind])
        descriptor[24 + 16] ^= 1
        sections[kind] = bytes(descriptor)
        with self.assertRaises(WireError):
            native.roundtrip_wire(
                encode_sections(
                    sections,
                    feature_flags=image.feature_flags,
                    minor=image.minor,
                )
            )

        sections = dict(image.sections)
        columns = bytearray(sections[kind])
        root_kinds_offset = struct.unpack_from("<Q", columns, 24 + 80)[0]
        columns[24 + root_kinds_offset] = 0xFF
        sections[kind] = bytes(columns)
        with self.assertRaises(WireError):
            native.roundtrip_wire(
                encode_sections(
                    sections,
                    feature_flags=image.feature_flags,
                    minor=image.minor,
                )
            )

    def test_new_minor_unknown_optional_and_verify_false_match_python_policy(self) -> None:
        encoded = encode_snapshot(snapshot("A"))
        image = read_wire(encoded)
        sections = dict(image.sections)
        sections[60_000] = b"opaque optional payload"
        compatible = encode_sections(sections, feature_flags=image.feature_flags, minor=41)
        validation = native.validate_wire(compatible)
        self.assertEqual(validation.wire_minor, 41)
        self.assertEqual(native.roundtrip_wire(compatible), compatible)

        digest_only = bytearray(encoded)
        digest_only[56] ^= 1
        with self.assertRaises(WireError):
            native.validate_wire(digest_only)
        self.assertEqual(native.roundtrip_wire(digest_only, verify=False), bytes(digest_only))

    def test_limits_cancellation_and_owned_mutable_buffer_publish_no_partial_result(self) -> None:
        source = snapshot(*(f"C{index}" for index in range(2_000)))
        encoded = encode_snapshot(source)
        with self.assertRaises(WireError):
            native.roundtrip_wire(
                encoded,
                limits=ParseLimits(max_wire_bytes=len(encoded) - 1),
            )
        with self.assertRaises(WireError):
            native.validate_wire(
                encoded,
                limits=ParseLimits(max_memory_bytes=len(encoded) + 76),
            )
        with self.assertRaises(WireError):
            native.validate_wire(encoded, limits=ParseLimits(max_strings=1))
        with self.assertRaises(WireError):
            native.validate_wire(encoded, limits=ParseLimits(max_axioms=1))

        mutable = bytearray(encoded)
        original = bytes(mutable)

        def mutate_after_gil_release() -> None:
            mutable[-1] ^= 1

        timer = threading.Timer(0.001, mutate_after_gil_release)
        timer.start()
        output = native.roundtrip_wire(mutable)
        timer.join(timeout=1.0)
        self.assertEqual(output, original)
        self.assertNotEqual(bytes(mutable), original)

        cancellation = CancellationSource()
        cancellation.cancel("test cancellation")
        with self.assertRaises(OperationCancelledError) as raised:
            native.roundtrip_wire(encoded, cancellation_token=cancellation.token)
        self.assertEqual(getattr(raised.exception, "code", None), "OPERATION_CANCELLED")


if __name__ == "__main__":
    unittest.main()
