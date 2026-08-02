from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError

import pyowl_core.model as m
from pyowl_core import (
    InvalidIRIError,
    InvalidLiteralError,
    ParseLimits,
    ResourceLimitError,
    StructuralConstraintError,
)


def _class(local: str) -> m.Class:
    return m.Class(m.IRI(f"https://example.org/{local}"))


class CanonicalIdentityTests(unittest.TestCase):
    def test_reference_bytes_for_iri_and_typed_entity(self) -> None:
        iri = m.IRI("https://example.org/A")
        iri_bytes = b"\x01\x02" + bytes((len(iri.value),)) + iri.value.encode()
        self.assertEqual(m.canonical_bytes(iri), iri_bytes)

        class_value = m.Class(iri)
        expected = b"\x02" + b"\x05\x05class" + b"\x01" + bytes((len(iri_bytes),)) + iri_bytes
        self.assertEqual(m.canonical_bytes(class_value), expected)
        self.assertEqual(
            m.structural_hexdigest(class_value),
            "f707f6da3af9c5229ebe815213055808a62990ccebab2d3885ebdfa851249b74",
        )
        self.assertEqual(
            hashlib.sha256(
                b"pyowl-core:structural-value:v1\x00\x01" + expected
            ).hexdigest(),
            "a552215a1bbdd4fc7e477d2af737482fe35ca0b5af0f19986b859656278ecf8f",
        )

    def test_unsigned_varints_are_minimal_and_arbitrary_precision(self) -> None:
        self.assertEqual(m.encode_varint(0), b"\x00")
        self.assertEqual(m.encode_varint(127), b"\x7f")
        self.assertEqual(m.encode_varint(128), b"\x80\x01")
        huge = 2**4096 + 17
        value = m.ObjectExactCardinality(
            huge,
            m.ObjectProperty(m.IRI("https://example.org/p")),
            _class("A"),
        )
        self.assertEqual(m.decode_canonical(m.canonical_bytes(value)), value)

    def test_decoder_rejects_noncanonical_and_trailing_encodings(self) -> None:
        encoded = m.canonical_bytes(m.IRI("https://example.org/A"))
        with self.assertRaises(StructuralConstraintError):
            m.decode_canonical(b"\x81\x00" + encoded[1:])
        with self.assertRaises(StructuralConstraintError):
            m.decode_canonical(encoded + b"\x00")

        entity = bytearray(m.canonical_bytes(_class("A")))
        self.assertEqual(entity[1], 5)
        entity[1] = 2
        with self.assertRaises(StructuralConstraintError):
            m.decode_canonical(bytes(entity))

    def test_unordered_values_flatten_deduplicate_and_sort(self) -> None:
        a, b, c = _class("A"), _class("B"), _class("C")
        left = m.ObjectIntersectionOf(m.CanonicalSet((a, b, c, a)))
        nested = m.ObjectIntersectionOf(
            m.CanonicalSet((m.ObjectIntersectionOf(m.CanonicalSet((c, a))), b))
        )
        right = m.ObjectIntersectionOf(m.CanonicalSet((c, b, a)))
        self.assertEqual(left, right)
        self.assertEqual(left, nested)
        self.assertEqual(hash(left), hash(right))
        self.assertEqual(tuple(left.operands), tuple(sorted((a, b, c))))
        with self.assertRaises(StructuralConstraintError):
            m.ObjectIntersectionOf(m.CanonicalSet((a, a)))

    def test_ordered_chains_retain_order_and_repetition(self) -> None:
        p = m.ObjectProperty(m.IRI("https://example.org/p"))
        q = m.ObjectProperty(m.IRI("https://example.org/q"))
        forward = m.ObjectPropertyChain((p, q, p))
        reverse = m.ObjectPropertyChain((p, p, q))
        self.assertNotEqual(forward, reverse)
        self.assertEqual(forward.properties, (p, q, p))

    def test_inverse_pair_is_symmetric_but_assertion_roles_are_not(self) -> None:
        p = m.ObjectProperty(m.IRI("https://example.org/p"))
        q = m.ObjectProperty(m.IRI("https://example.org/q"))
        self.assertEqual(m.InverseObjectProperties(p, q), m.InverseObjectProperties(q, p))
        a = m.NamedIndividual(m.IRI("https://example.org/a"))
        b = m.NamedIndividual(m.IRI("https://example.org/b"))
        self.assertNotEqual(
            m.ObjectPropertyAssertion(p, a, b),
            m.ObjectPropertyAssertion(p, b, a),
        )

    def test_language_identity_and_lexical_provenance_are_separate(self) -> None:
        upper = m.Literal("hello", m.RDF_PLAIN_LITERAL, "EN-gb")
        lower = m.Literal("hello", m.RDF_PLAIN_LITERAL, "en-GB")
        grandfathered = m.Literal("hello", m.RDF_PLAIN_LITERAL, "ZH-MIN-NAN")
        self.assertEqual(upper, lower)
        self.assertEqual(upper.language, "en-gb")
        self.assertEqual(grandfathered.language, "zh-min-nan")

        builder = m.DocumentBuilder("https://example.org/document")
        builder.attach_lexical(upper, "language-tag", "EN-gb")
        provenance = builder.freeze_lexical_provenance()
        self.assertEqual(provenance.tokens_for(lower)[0].spelling, "EN-gb")

    def test_language_and_datatype_constraints_reject_invalid_combinations(self) -> None:
        for language in ("en-a-foo-a-bar", "sl-rozaj-rozaj", "en_uk", ""):
            with self.subTest(language=language), self.assertRaises(InvalidLiteralError):
                m.Literal("hello", m.RDF_PLAIN_LITERAL, language)
        with self.assertRaises(InvalidLiteralError):
            m.Literal("hello", m.XSD_STRING, "en")
        rdf_lang_string = m.Datatype(m.IRI(m.RDF_LANG_STRING_IRI))
        with self.assertRaises(InvalidLiteralError):
            m.Literal("hello", rdf_lang_string)
        with self.assertRaises(InvalidLiteralError):
            m.Literal("\ud800", m.XSD_STRING)

    def test_iri_rejects_relative_malformed_escape_and_ill_formed_unicode(self) -> None:
        for value in ("relative", "https://example.org/%zz", "https://example.org/\ud800"):
            with self.subTest(value=ascii(value)), self.assertRaises(InvalidIRIError):
                m.IRI(value)

    def test_iri_identity_does_not_normalize_unicode_or_percent_escapes(self) -> None:
        composed = m.IRI("https://example.org/\N{LATIN SMALL LETTER E WITH ACUTE}")
        decomposed = m.IRI("https://example.org/e\N{COMBINING ACUTE ACCENT}")
        escaped = m.IRI("https://example.org/%C3%A9")
        self.assertNotEqual(composed, decomposed)
        self.assertNotEqual(composed, escaped)
        self.assertEqual(str(composed), composed.value)

    def test_annotations_are_recursive_structural_identity(self) -> None:
        property = m.AnnotationProperty(m.IRI("https://example.org/label"))
        nested = m.Annotation(property, m.IRI("https://example.org/source"))
        annotated = m.Annotation(
            property, m.Literal("label", m.XSD_STRING), m.CanonicalSet((nested,))
        )
        plain = m.Annotation(property, m.Literal("label", m.XSD_STRING))
        self.assertNotEqual(annotated, plain)
        self.assertEqual(m.decode_canonical(m.canonical_bytes(annotated)), annotated)

    def test_cardinality_rejects_bool_and_negative_without_narrowing(self) -> None:
        property = m.ObjectProperty(m.IRI("https://example.org/p"))
        for cardinality in (True, -1):
            with (
                self.subTest(cardinality=cardinality),
                self.assertRaises((TypeError, StructuralConstraintError)),
            ):
                m.ObjectMinCardinality(cardinality, property, _class("A"))

    def test_canonicalization_does_not_apply_reasoner_rewrites(self) -> None:
        a = _class("A")
        double_complement = m.ObjectComplementOf(m.ObjectComplementOf(a))
        self.assertNotEqual(double_complement, a)
        equivalent = m.EquivalentClasses(m.CanonicalSet((a, m.OWL_THING)))
        inclusion = m.SubClassOf(a, m.OWL_THING)
        self.assertNotEqual(equivalent, inclusion)

    def test_values_are_frozen_and_cycles_fail_safely(self) -> None:
        value = m.ObjectComplementOf(_class("A"))
        with self.assertRaises(FrozenInstanceError):
            value.operand = _class("B")  # type: ignore[misc]
        object.__setattr__(value, "operand", value)
        with self.assertRaises(StructuralConstraintError):
            m.canonical_bytes(value)
        with self.assertRaises(StructuralConstraintError):
            tuple(m.walk(value))

    def test_resource_limits_bound_depth_and_collection_arity(self) -> None:
        nested: m.ClassExpression = _class("A")
        for _ in range(8):
            nested = m.ObjectComplementOf(nested)
        with self.assertRaises(ResourceLimitError):
            m.canonical_bytes(nested, limits=ParseLimits(max_nesting_depth=4))

        p = m.ObjectProperty(m.IRI("https://example.org/p"))
        chain = m.ObjectPropertyChain((p, p, p))
        with self.assertRaises(ResourceLimitError):
            m.canonical_bytes(chain, limits=ParseLimits(max_sequence_arity=2))

    def test_default_depth_boundary_succeeds_then_fails_without_recursion_error(self) -> None:
        near_limit: m.ClassExpression = _class("A")
        for _ in range(510):
            near_limit = m.ObjectComplementOf(near_limit)
        encoded = m.canonical_bytes(near_limit)
        self.assertEqual(m.decode_canonical(encoded), near_limit)

        beyond_limit = m.ObjectComplementOf(m.ObjectComplementOf(near_limit))
        with self.assertRaises(ResourceLimitError):
            m.canonical_bytes(beyond_limit)

    def test_hash_does_not_depend_on_python_hash_seed(self) -> None:
        code = (
            "from pyowl_core.model import IRI, Class; "
            "print(hash(Class(IRI('https://example.org/A'))))"
        )
        outputs = []
        for seed in ("1", "987654"):
            environment = dict(os.environ, PYTHONHASHSEED=seed)
            outputs.append(
                subprocess.check_output(
                    [sys.executable, "-c", code],
                    env=environment,
                    text=True,
                ).strip()
            )
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
