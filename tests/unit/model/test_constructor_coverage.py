from __future__ import annotations

import json
import unittest
from dataclasses import fields
from pathlib import Path

import pyowl_core.model as m
from pyowl_core import PyOWLCoreError
from tests.generated.model.fixtures import model_fixtures, typed_entity_fixtures
from tests.generated.model.generate_coverage import OUTPUT, render_coverage
from tools.schema.tags import TagLedger

ROOT = Path(__file__).resolve().parents[3]


class ConstructorCoverageTests(unittest.TestCase):
    def test_fixture_inventory_is_exactly_the_permanent_registry(self) -> None:
        fixtures = model_fixtures()
        self.assertEqual(set(fixtures), set(m.MODEL_CONSTRUCTORS))
        self.assertEqual(len(fixtures), 76)
        for constructor, value in fixtures.items():
            with self.subTest(constructor=constructor.__name__):
                self.assertIs(m.constructor_spec(value).constructor, constructor)

    def test_every_typed_entity_branch_has_generic_and_typed_constructor_parity(self) -> None:
        constructors = {
            m.EntityKind.CLASS: m.Class,
            m.EntityKind.DATATYPE: m.Datatype,
            m.EntityKind.OBJECT_PROPERTY: m.ObjectProperty,
            m.EntityKind.DATA_PROPERTY: m.DataProperty,
            m.EntityKind.ANNOTATION_PROPERTY: m.AnnotationProperty,
            m.EntityKind.NAMED_INDIVIDUAL: m.NamedIndividual,
        }
        for kind, typed in typed_entity_fixtures().items():
            constructor = constructors[kind]
            with self.subTest(kind=kind):
                generic = m.Entity(kind, typed.iri)
                self.assertIsInstance(generic, constructor)
                self.assertEqual(generic, typed)
                self.assertEqual(m.decode_canonical(m.canonical_bytes(generic)), typed)

    def test_every_constructor_round_trips_and_has_stable_identity(self) -> None:
        for constructor, value in model_fixtures().items():
            with self.subTest(constructor=constructor.__name__):
                encoded = m.canonical_bytes(value)
                decoded = m.decode_canonical(encoded)
                self.assertEqual(decoded, value)
                self.assertEqual(hash(decoded), hash(value))
                self.assertEqual(m.canonical_bytes(decoded), encoded)
                self.assertEqual(m.structural_digest(decoded), m.structural_digest(value))
                self.assertTrue(encoded.startswith(m.encode_varint(m.constructor_spec(value).tag)))

    def test_every_axiom_branch_retains_nested_annotations(self) -> None:
        fixtures = model_fixtures()
        for spec in m.CONSTRUCTOR_SPECS:
            if not spec.category.endswith("axiom"):
                continue
            value = fixtures[spec.constructor]
            with self.subTest(constructor=spec.constructor.__name__):
                self.assertIsInstance(value, m.AxiomNode)
                self.assertTrue(value.annotations)  # type: ignore[attr-defined]
                annotation = next(iter(value.annotations))  # type: ignore[attr-defined]
                self.assertTrue(annotation.annotations)

    def test_every_constructor_rejects_an_invalid_first_field(self) -> None:
        for constructor, value in model_fixtures().items():
            values = [getattr(value, field.name) for field in fields(constructor)]
            values[0] = object()
            with (
                self.subTest(constructor=constructor.__name__),
                self.assertRaises((TypeError, ValueError, PyOWLCoreError)),
            ):
                constructor(*values)

    def test_generated_production_coverage_is_current_and_exhaustive(self) -> None:
        self.assertEqual(OUTPUT.read_text(encoding="utf-8"), render_coverage())
        coverage = json.loads(render_coverage())
        rows = coverage["rows"]
        self.assertEqual(coverage["constructor_count"], len(m.CONSTRUCTOR_SPECS))
        self.assertEqual(coverage["production_count"], len(m.CONSTRUCTOR_SPECS) + 5)
        self.assertEqual(
            [row["constructor"] for row in rows],
            [spec.constructor.__name__ for spec in m.CONSTRUCTOR_SPECS],
        )
        self.assertTrue(
            all(row["normative_source"].startswith("https://www.w3.org/") for row in rows)
        )
        entity_row = next(row for row in rows if row["constructor"] == "Entity")
        self.assertEqual(len(entity_row["productions"]), len(m.EntityKind))

    def test_model_schema_and_generated_constants_are_exactly_current(self) -> None:
        ledger = TagLedger.load(ROOT / "schemas" / "model-v1.toml")
        generated = ROOT / "src" / "pyowl_core" / "model" / "_tags.py"
        import pyowl_core

        self.assertEqual(ledger.schema, pyowl_core.MODEL_SCHEMA_VERSION)
        self.assertEqual(generated.read_text(encoding="utf-8"), ledger.render_python())
        active = {(tag.name, tag.value) for tag in ledger.tags if tag.status == "active"}
        registered = {(spec.tag_name, spec.tag) for spec in m.CONSTRUCTOR_SPECS}
        self.assertEqual(active, registered)

    def test_swrl_is_public_only_in_the_explicit_extension_namespace(self) -> None:
        import pyowl_core
        import pyowl_core.extensions.swrl as swrl

        for name in swrl.__all__:
            self.assertNotIn(name, m.__all__)
            self.assertNotIn(name, pyowl_core.__all__)

    def test_exhaustive_visitor_dispatch_and_unknown_failure(self) -> None:
        methods = {
            method_name: (lambda self, value, name=method_name: name)
            for method_name in m.VISITOR_METHODS
        }
        exhaustive_visitor = type("ExhaustiveVisitor", (m.NodeVisitor,), methods)()
        fixtures = model_fixtures()
        for method_name, constructor in zip(
            m.VISITOR_METHODS,
            m.MODEL_CONSTRUCTORS,
            strict=True,
        ):
            value = fixtures[constructor]
            with self.subTest(constructor=constructor.__name__):
                self.assertEqual(exhaustive_visitor.visit(value), method_name)

        with self.assertRaises(m.UnknownNodeError):
            m.NodeVisitor[object]().visit(fixtures[m.IRI])

    def test_functional_dispatch_handles_typed_entities_as_entity(self) -> None:
        fixtures = model_fixtures()
        handlers = {
            constructor: (lambda value, name=constructor.__name__: name)
            for constructor in m.MODEL_CONSTRUCTORS
        }
        for constructor, value in fixtures.items():
            self.assertEqual(m.visit_node(value, handlers), constructor.__name__)


if __name__ == "__main__":
    unittest.main()
