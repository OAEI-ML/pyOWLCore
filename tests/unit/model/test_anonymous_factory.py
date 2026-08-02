from __future__ import annotations

import unittest

import pyowl_core.model as m
from pyowl_core import ParseLimits, ResourceLimitError


class AnonymousAndFactoryTests(unittest.TestCase):
    def test_symmetric_alpha_canonicalization_is_label_independent(self) -> None:
        scope = m.canonical_document_scope("https://example.org/document")
        first = m.alpha_canonicalize_blank_nodes(
            (
                m.BlankNodeArc("left", "edge", "right", b"payload"),
                m.BlankNodeArc("right", "edge", "left", b"payload"),
            ),
            scope,
        )
        renamed = m.alpha_canonicalize_blank_nodes(
            (
                m.BlankNodeArc("z", "edge", "a", b"payload"),
                m.BlankNodeArc("a", "edge", "z", b"payload"),
            ),
            scope,
        )
        self.assertEqual(first.canonical_graph, renamed.canonical_graph)
        self.assertEqual(
            sorted(binding.individual.local_key for binding in first.bindings),
            sorted(binding.individual.local_key for binding in renamed.bindings),
        )
        self.assertEqual(
            first.canonical_graph.hex(),
            "70796f776c2d636f72653a626c616e6b2d67726170683a7632000202100004656467650101077061796c6f6164100104656467650100077061796c6f6164",
        )

    def test_cross_document_scopes_standardize_anonymous_values_apart(self) -> None:
        arcs = (m.BlankNodeArc("x", "type", payload=b"Class"),)
        first = m.alpha_canonicalize_blank_nodes(
            arcs,
            m.canonical_document_scope("https://example.org/first"),
        )
        second = m.alpha_canonicalize_blank_nodes(
            arcs,
            m.canonical_document_scope("https://example.org/second"),
        )
        self.assertNotEqual(first.bindings[0].individual, second.bindings[0].individual)

    def test_alpha_tie_work_is_bounded_before_permutation(self) -> None:
        scope = m.canonical_document_scope("https://example.org/document")
        with self.assertRaises(ResourceLimitError):
            m.alpha_canonicalize_blank_nodes(
                (),
                scope,
                labels=("a", "b", "c", "d"),
                limits=ParseLimits(max_canonical_work=23),
            )

    def test_document_builder_owns_scope_and_records_explicit_rescope(self) -> None:
        first = m.DocumentBuilder("https://example.org/first")
        same_scope = m.DocumentBuilder("https://example.org/first")
        second = m.DocumentBuilder("https://example.org/second")
        keyed = first.anonymous("stable")
        self.assertIs(keyed, first.anonymous("stable"))
        self.assertEqual(keyed, same_scope.anonymous("stable"))
        self.assertNotEqual(keyed, second.anonymous("stable"))
        self.assertNotEqual(first.anonymous(), first.anonymous())

        moved, record = second.re_scope(keyed)
        self.assertEqual(moved.document_scope, second.document_scope)
        self.assertEqual(record.old_scope, first.document_scope)
        self.assertEqual(record.new_scope, second.document_scope)
        self.assertNotEqual(moved, keyed)

    def test_factory_interning_is_caller_owned_and_optional(self) -> None:
        first = m.OWLFactory()
        second = m.OWLFactory()
        retained = first.class_("https://example.org/A")
        self.assertIs(retained, first.class_("https://example.org/A"))
        independent = second.class_("https://example.org/A")
        self.assertEqual(retained, independent)
        self.assertIsNot(retained, independent)
        self.assertEqual(first.stats(), m.FactoryStats(retained_values=2, hits=2, misses=2))
        self.assertIsInstance(first.entity(m.EntityKind.CLASS, "https://example.org/B"), m.Class)


if __name__ == "__main__":
    unittest.main()
