from __future__ import annotations

import subprocess
import sys
import unittest
import warnings
from unittest.mock import patch

from pyowl_core.backends import dispatch
from pyowl_core.backends.native import NativeProbe
from pyowl_core.config import BackendPreference
from pyowl_core.exceptions import BackendUnavailableError, NativeBackendUnavailableWarning


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        dispatch._reset_warnings_for_tests()

    def test_explicit_python_is_silent_and_does_not_probe_native(self) -> None:
        with (
            patch("pyowl_core.backends.dispatch.native.probe") as probe,
            warnings.catch_warnings(record=True) as observed,
        ):
            selected = dispatch.select_backend(
                BackendPreference.PYTHON,
                capability="wire-v1",
                operation="wire encode",
            )
        self.assertEqual(selected.backend, "python")
        probe.assert_not_called()
        self.assertEqual(observed, [])

    def test_auto_selects_compatible_native_before_work(self) -> None:
        with patch(
            "pyowl_core.backends.dispatch.native.probe",
            return_value=NativeProbe(True, None, "0.1.0-dev.0", ("wire-v1",)),
        ):
            selected = dispatch.select_backend(
                BackendPreference.AUTO,
                capability="wire-v1",
                operation="wire encode",
            )
        self.assertEqual(selected.backend, "native")
        self.assertEqual(selected.native_version, "0.1.0-dev.0")

    def test_forced_native_never_falls_back(self) -> None:
        with (
            patch(
                "pyowl_core.backends.dispatch.native.probe",
                return_value=NativeProbe(False, "native extension is not installed", None, ()),
            ),
            self.assertRaises(BackendUnavailableError) as raised,
        ):
            dispatch.select_backend(
                BackendPreference.NATIVE,
                capability="wire-v1",
                operation="wire decode",
            )
        self.assertEqual(raised.exception.code, "NATIVE_BACKEND_UNAVAILABLE")

    def test_auto_warns_once_per_sanitized_reason(self) -> None:
        missing = NativeProbe(False, "native extension is not installed", None, ())
        incompatible = NativeProbe(False, "native ABI is incompatible", None, ())
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            with patch("pyowl_core.backends.dispatch.native.probe", return_value=missing):
                for operation in ("wire encode", "wire decode"):
                    selected = dispatch.select_backend(
                        BackendPreference.AUTO,
                        capability="wire-v1",
                        operation=operation,
                    )
                    self.assertEqual(selected.backend, "python")
            with patch("pyowl_core.backends.dispatch.native.probe", return_value=incompatible):
                dispatch.select_backend(
                    BackendPreference.AUTO,
                    capability="wire-v1",
                    operation="wire validate",
                )
        native_warnings = [
            item for item in observed if issubclass(item.category, NativeBackendUnavailableWarning)
        ]
        self.assertEqual(len(native_warnings), 2)
        self.assertIn("selected the complete Python backend", str(native_warnings[0].message))
        self.assertIn("BackendPreference.PYTHON", str(native_warnings[0].message))

    def test_pure_python_backend_never_imports_native_modules(self) -> None:
        script = """
import sys
import pyowl_core.backends.python
assert 'pyowl_core._native' not in sys.modules
assert 'pyowl_core.backends.native' not in sys.modules
"""
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
