from __future__ import annotations

import unittest

import pyowl_core
from pyowl_core import Diagnostic, OntologySyntaxError, Severity, SourceSpan


class DiagnosticTests(unittest.TestCase):
    def test_diagnostic_round_trip_is_immutable_and_caller_isolated(self) -> None:
        details = {"count": 2, "recoverable": False}
        diagnostic = Diagnostic(
            code="ONTOLOGY_SYNTAX",
            severity=Severity.ERROR,
            message="unexpected token",
            document_iri="file:///ontology.owl",
            source_span=SourceSpan(byte_start=3, byte_end=5, line_start=1, column_start=4),
            import_chain=("file:///root.owl",),
            details=details,
        )
        details["count"] = 99
        self.assertEqual(diagnostic.details["count"], 2)
        self.assertEqual(Diagnostic.from_dict(diagnostic.to_dict()), diagnostic)
        with self.assertRaises(TypeError):
            diagnostic.details["count"] = 3  # type: ignore[index]

    def test_exception_code_round_trips_through_diagnostic(self) -> None:
        error = OntologySyntaxError("bad syntax")
        diagnostic = error.as_diagnostic()
        self.assertEqual(diagnostic.code, "ONTOLOGY_SYNTAX")
        rebuilt = OntologySyntaxError(diagnostic.message, diagnostic=diagnostic)
        self.assertEqual(rebuilt.code, error.code)
        self.assertEqual(rebuilt.as_diagnostic(), diagnostic)

    def test_public_error_and_warning_taxonomy_is_constructible(self) -> None:
        error_names = {
            "AccessDeniedError",
            "AdapterCompatibilityError",
            "AdapterError",
            "BackendError",
            "BackendProtocolError",
            "BackendUnavailableError",
            "ClosedSnapshotError",
            "DeltaBaseMismatchError",
            "DeltaError",
            "DocumentIdentityConflictError",
            "FormatDetectionError",
            "ImportCycleError",
            "ImportResolutionError",
            "IntegrityError",
            "InvalidIRIError",
            "InvalidLiteralError",
            "ModelError",
            "OntologySyntaxError",
            "OperationCancelledError",
            "OptionConflictError",
            "ParseError",
            "ProfileError",
            "PyOWLCoreError",
            "ReentrancyError",
            "ResourceLimitError",
            "SnapshotInUseError",
            "SnapshotLifecycleError",
            "StructuralConstraintError",
            "UnresolvedImportError",
            "UnsupportedSyntaxError",
            "WireCorruptionError",
            "WireError",
            "WireLimitError",
            "WireVersionError",
        }
        for name in error_names:
            with self.subTest(name=name):
                error_type = getattr(pyowl_core, name)
                error = (
                    error_type(
                        "test",
                        limit="max_terms",
                        observed=2,
                        allowed=1,
                    )
                    if name == "ResourceLimitError"
                    else error_type("test")
                )
                self.assertIsInstance(error, pyowl_core.PyOWLCoreError)
                self.assertRegex(error.code, r"^[A-Z][A-Z0-9_]*$")

        for name in (
            "DeprecatedAPIWarning",
            "FormatGuessWarning",
            "LossyRenderWarning",
            "NativeBackendUnavailableWarning",
            "UnresolvedImportWarning",
        ):
            with self.subTest(name=name):
                self.assertTrue(issubclass(getattr(pyowl_core, name), Warning))

    def test_invalid_diagnostic_data_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Diagnostic(code="bad-code", severity=Severity.ERROR, message="bad")
        with self.assertRaises(ValueError):
            SourceSpan(byte_start=4, byte_end=3)
        with self.assertRaises(TypeError):
            Diagnostic(
                code="BAD_DETAIL",
                severity=Severity.ERROR,
                message="bad",
                details={"object": object()},  # type: ignore[dict-item]
            )

    def test_resource_limit_contract_is_typed_and_immutable(self) -> None:
        details = {"component_count": 3, "work_term": "refinement"}
        error = pyowl_core.ResourceLimitError(
            "canonical work exceeded",
            limit="max_canonical_work",
            observed=11,
            allowed=10,
            details=details,
        )
        details["component_count"] = 99

        self.assertEqual(error.limit, "max_canonical_work")
        self.assertEqual(error.observed, 11)
        self.assertEqual(error.allowed, 10)
        self.assertEqual(error.details["component_count"], 3)
        self.assertEqual(error.as_diagnostic().details, error.details)
        with self.assertRaises(TypeError):
            error.details["component_count"] = 4  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
