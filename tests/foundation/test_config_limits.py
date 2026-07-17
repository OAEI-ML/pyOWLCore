from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    ParseLimits,
    ResourceLimitError,
)


class ConfigContractTests(unittest.TestCase):
    def test_enum_values_and_secure_defaults_are_frozen(self) -> None:
        self.assertEqual(DocumentFormat.RDF_XML.value, "rdfxml")
        self.assertEqual(DocumentFormat.TURTLE.value, "turtle")
        self.assertEqual(DocumentFormat.OWL_XML.value, "owlxml")
        self.assertEqual(DocumentFormat.FUNCTIONAL.value, "functional")
        self.assertEqual(ImportPolicy.RESOLVE_LOCAL.value, "resolve_local")
        self.assertEqual(BackendPreference.AUTO.value, "auto")

        options = LoadOptions()
        self.assertTrue(options.offline)
        self.assertEqual(options.imports, ImportPolicy.RESOLVE_LOCAL)
        self.assertEqual(options.backend, BackendPreference.AUTO)
        self.assertTrue(options.collect_provenance)
        self.assertTrue(options.deterministic)
        with self.assertRaises(FrozenInstanceError):
            options.offline = False  # type: ignore[misc]

    def test_default_limits_are_independent_immutable_values(self) -> None:
        first = LoadOptions()
        second = LoadOptions()
        self.assertEqual(first.limits, second.limits)
        self.assertIsNot(first.limits, second.limits)
        self.assertEqual(first.limits.max_source_bytes, 2 * 1024**3)
        self.assertEqual(first.limits.max_documents, 1_000)
        with self.assertRaises(FrozenInstanceError):
            first.limits.max_documents = 2  # type: ignore[misc]

    def test_invalid_configuration_types_fail_early(self) -> None:
        with self.assertRaises(TypeError):
            LoadOptions(imports="resolve_local")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            LoadOptions(offline=1)  # type: ignore[arg-type]
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ParseLimits(max_documents=value)
        for value in (0.0, math.inf, math.nan, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ParseLimits(deadline_seconds=value)

    def test_limit_enforcement_and_tightening_are_explicit(self) -> None:
        generous = ParseLimits(max_documents=10, max_memory_bytes=None)
        strict = ParseLimits(max_documents=3, max_memory_bytes=100)
        tightened = generous.tightened_with(strict)
        self.assertEqual(tightened.max_documents, 3)
        self.assertEqual(tightened.max_memory_bytes, 100)
        tightened.enforce("max_documents", 3)
        with self.assertRaises(ResourceLimitError) as caught:
            tightened.enforce("max_documents", 4)
        self.assertEqual(caught.exception.limit, "max_documents")
        self.assertEqual(caught.exception.observed, 4)
        self.assertEqual(caught.exception.allowed, 3)
        self.assertNotIn("hostile", str(caught.exception))
        with self.assertRaises(KeyError):
            tightened.enforce("max_document", 1)


if __name__ == "__main__":
    unittest.main()
