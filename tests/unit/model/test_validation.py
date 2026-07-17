from __future__ import annotations

import unittest

import pyowl_core.model as m
from tests.generated.model.fixtures import model_fixtures


class ValidationFoundationTests(unittest.TestCase):
    def test_structural_report_covers_exhaustive_values(self) -> None:
        report = m.validate_structural(model_fixtures().values())
        self.assertTrue(report.valid)
        self.assertTrue(report.complete)
        self.assertGreaterEqual(report.values_checked, len(m.MODEL_CONSTRUCTORS))

    def test_role_analysis_propagates_non_simple_status_to_superproperties(self) -> None:
        p = m.ObjectProperty(m.IRI("https://example.org/p"))
        q = m.ObjectProperty(m.IRI("https://example.org/q"))
        analysis = m.analyze_object_property_roles(
            (m.TransitiveObjectProperty(p), m.SubObjectPropertyOf(p, q))
        )
        self.assertFalse(analysis.is_simple(p))
        self.assertFalse(analysis.is_simple(q))
        self.assertFalse(analysis.is_simple(m.inverse_property(p)))

    def test_owl2_dl_foundation_reports_punning_declarations_and_incompleteness(self) -> None:
        iri = m.IRI("https://example.org/punned")
        class_value = m.Class(iri)
        datatype_value = m.Datatype(iri)
        report = m.validate_owl2_dl((m.Declaration(class_value), m.Declaration(datatype_value)))
        codes = {issue.code for issue in report.issues}
        self.assertIn("OWL2DL_CLASS_DATATYPE_PUNNING", codes)
        self.assertIn("OWL2DL_CLOSURE_VALIDATION_PENDING", codes)
        self.assertFalse(report.complete)
        self.assertFalse(report.conforms)

    def test_profile_interface_is_explicitly_incomplete_until_closure_validation(self) -> None:
        report = m.validate_profile((), m.Profile.EL)
        self.assertEqual(report.profile, m.Profile.EL)
        self.assertFalse(report.complete)
        self.assertFalse(report.conforms)
        self.assertEqual(report.issues[0].code, "PROFILE_CLOSURE_VALIDATION_PENDING")

    def test_literal_lexical_validation_is_policy_separate_from_construction(self) -> None:
        integer = m.Datatype(m.IRI("http://www.w3.org/2001/XMLSchema#integer"))
        malformed = m.Literal("not-an-integer", integer)
        issues = m.validate_lexical_form(malformed)
        self.assertEqual(tuple(issue.code for issue in issues), ("INVALID_DATATYPE_LEXICAL_FORM",))

        unsigned_byte = m.Datatype(m.IRI("http://www.w3.org/2001/XMLSchema#unsignedByte"))
        self.assertEqual(
            m.validate_lexical_form(m.Literal("256", unsigned_byte))[0].code,
            "INVALID_DATATYPE_LEXICAL_FORM",
        )
        custom = m.Datatype(m.IRI("https://example.org/integer"))
        self.assertEqual(m.validate_lexical_form(m.Literal("free-form", custom)), ())


if __name__ == "__main__":
    unittest.main()
