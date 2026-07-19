from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar, cast
from unittest.mock import patch

from pyowl_core import ParseLimits, canonical_bytes
from pyowl_core.backends import native
from pyowl_core.exceptions import PyOWLCoreError, WireCorruptionError, WireError, WireLimitError
from pyowl_core.extensions.swrl import Variable
from pyowl_core.model import (
    IRI,
    RDF_PLAIN_LITERAL,
    Literal,
    ObjectPropertyChain,
    constructor_spec,
    decode_canonical,
)
from tests.generated.model.fixtures import model_fixtures
from tests.native.foundation._support import NativeTestExtension, load_extension


class NativeBoundaryTests(unittest.TestCase):
    extension: ClassVar[NativeTestExtension]

    @classmethod
    def setUpClass(cls) -> None:
        cls.extension = load_extension()
        native._reset_probe_cache_for_tests()
        result = native.probe(refresh=True)
        if not result.available:
            raise unittest.SkipTest(result.reason or "native extension is unavailable")

    def test_exact_versions_features_and_self_test(self) -> None:
        extension = self.extension
        self.assertEqual(extension.ABI_VERSION, 1)
        self.assertEqual(extension.MODEL_SCHEMA_VERSION, 1)
        self.assertEqual(extension.WIRE_FORMAT_VERSION, (1, 1))
        self.assertEqual(extension.FEATURES, tuple(sorted(set(extension.FEATURES))))
        self.assertIn("safe-rust", extension.FEATURES)
        self.assertNotIn("parse-functional", extension.FEATURES)
        extension.self_test()

    def test_complete_limit_ledger_is_frozen_and_enforced(self) -> None:
        config = native._encode_config(ParseLimits(), None, verify=True)
        self.assertEqual(len(config), 312)
        self.assertEqual(native._CONFIG.unpack(config)[0:4], (b"PYNCONF\0", 1, 1, 0))

        iri = canonical_bytes(IRI("urn:native-limit"))
        with self.assertRaises(WireLimitError):
            native.validate_canonical(iri, limits=ParseLimits(max_iri_bytes=4))

        literal = canonical_bytes(Literal("native-limit", RDF_PLAIN_LITERAL))
        with self.assertRaises(WireLimitError):
            native.validate_canonical(literal, limits=ParseLimits(max_literal_bytes=4))

        with self.assertRaises(WireLimitError):
            native.validate_canonical(
                iri,
                limits=ParseLimits(max_canonical_work=len(iri) - 1),
            )

    def test_every_model_constructor_has_canonical_byte_parity(self) -> None:
        fixtures = model_fixtures()
        self.assertEqual(len(fixtures), 76)
        for constructor, value in fixtures.items():
            with self.subTest(constructor=constructor.__name__):
                expected = canonical_bytes(value)
                self.assertEqual(native.validate_canonical(memoryview(expected)), expected)

    def test_every_model_constructor_roundtrips_retained_component(self) -> None:
        if not hasattr(self.extension, "_component_roundtrip_v1"):
            if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
                self.fail("selected native test-hooks artifact lacks _component_roundtrip_v1")
            self.skipTest("native retained-component test hook is unavailable")
        config = native._encode_config(ParseLimits(), None, verify=True)
        fixtures = model_fixtures()
        self.assertEqual(len(fixtures), 76)
        self.assertEqual(constructor_spec(fixtures[ObjectPropertyChain]).tag, 11)
        self.assertEqual(constructor_spec(fixtures[Variable]).tag, 140)
        for constructor, value in fixtures.items():
            with self.subTest(
                constructor=constructor.__name__,
                tag=constructor_spec(value).tag,
            ):
                expected = canonical_bytes(value)
                self.assertEqual(
                    self.extension._component_roundtrip_v1(memoryview(expected), config, None),
                    expected,
                )

    def test_retained_component_hook_propagates_cancellation(self) -> None:
        if not hasattr(self.extension, "_component_roundtrip_v1"):
            if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
                self.fail("selected native test-hooks artifact lacks _component_roundtrip_v1")
            self.skipTest("native retained-component test hook is unavailable")
        config = native._encode_config(ParseLimits(), None, verify=True)
        value = canonical_bytes(next(iter(model_fixtures().values())))
        cancel = self.extension._Cancellation(None)
        cancel.cancel()
        with self.assertRaises(self.extension._NativeError) as raised:
            self.extension._component_roundtrip_v1(memoryview(value), config, cancel)
        self.assertEqual(raised.exception.args[0], "NATIVE_CANCELLED")

    def test_every_constructor_byte_mutation_matches_python_acceptance(self) -> None:
        def python_accepts(value: bytes) -> bool:
            try:
                decode_canonical(value)
            except PyOWLCoreError:
                return False
            return True

        def native_accepts(value: bytes) -> bool:
            try:
                native.validate_canonical(value)
            except WireError:
                return False
            return True

        for constructor, fixture in model_fixtures().items():
            encoded = canonical_bytes(fixture)
            variants = [encoded[:length] for length in range(len(encoded))]
            for offset in range(len(encoded)):
                mutated = bytearray(encoded)
                mutated[offset] ^= 1
                variants.append(bytes(mutated))
            for variant in variants:
                self.assertEqual(
                    native_accepts(variant),
                    python_accepts(variant),
                    f"canonical mutation parity failed for {constructor.__name__}: {variant.hex()}",
                )

    def test_hostile_canonical_value_is_typed_and_panic_is_contained(self) -> None:
        hostile = (
            b"\x81\x00\x02\x01x",  # nonminimal tag
            b"\x01\x04\x00",  # IRI field encoded as an integer
            b"\x01\x02\x01x",  # relative IRI
            b"\x18\x06\x00",  # empty DataOneOf
            b"\x0a\x01\x08\x01\x02\x05urn:x",  # inverse of an IRI, not a property
        )
        for value in hostile:
            with self.subTest(value=value), self.assertRaises(WireCorruptionError):
                native.validate_canonical(value)

        language_literal = Literal("hello", RDF_PLAIN_LITERAL, "en-GB")
        encoded = canonical_bytes(language_literal)
        self.assertEqual(native.validate_canonical(encoded), encoded)
        uppercase = encoded.replace(b"en-gb", b"EN-gb")
        self.assertNotEqual(uppercase, encoded)
        with self.assertRaises(WireCorruptionError):
            native.validate_canonical(uppercase)

        with self.assertRaises(self.extension._NativeError) as raised:
            self.extension._panic_probe()
        self.assertEqual(raised.exception.args[0], "NATIVE_PANIC")

    def test_owned_input_is_thread_safe_and_no_borrow_escapes(self) -> None:
        payload = bytearray(canonical_bytes(next(iter(model_fixtures().values()))))

        def validate() -> bytes:
            return cast(bytes, native.validate_canonical(payload))

        with ThreadPoolExecutor(max_workers=8) as workers:
            outputs = tuple(workers.map(lambda _index: validate(), range(64)))
        self.assertTrue(all(value == bytes(payload) for value in outputs))

    def test_long_native_work_releases_gil_and_polls_atomic_cancellation(self) -> None:
        config = native._encode_config(ParseLimits(max_terms=500_000_000), None, verify=True)
        cancel = self.extension._Cancellation(None)
        started = threading.Event()
        counter = 0

        def peer() -> None:
            nonlocal counter
            started.set()
            while not cancel.cancelled:
                counter += 1

        thread = threading.Thread(target=peer)
        thread.start()
        started.wait(timeout=1.0)
        timer = threading.Timer(0.02, cancel.cancel)
        timer.start()
        with self.assertRaises(self.extension._NativeError) as raised:
            self.extension._work_probe(500_000_000, config, cancel)
        timer.join(timeout=1.0)
        thread.join(timeout=1.0)
        self.assertEqual(raised.exception.args[0], "NATIVE_CANCELLED")
        self.assertGreater(counter, 0)

    def test_deadline_has_stable_error_code(self) -> None:
        limits = ParseLimits(deadline_seconds=0.001, max_terms=500_000_000)
        config = native._encode_config(limits, None, verify=True)
        cancel = self.extension._Cancellation(None)
        with self.assertRaises(self.extension._NativeError) as raised:
            self.extension._work_probe(500_000_000, config, cancel)
        self.assertEqual(raised.exception.args[0], "NATIVE_DEADLINE")

        with self.assertRaises(ValueError):
            self.extension._Cancellation(1e308)

    @unittest.skipUnless(os.name == "posix", "signal probe requires POSIX signals")
    def test_sigint_is_checked_during_gil_released_native_work(self) -> None:
        script = """
from pyowl_core import ParseLimits
from pyowl_core.backends import native
from tests.native.foundation._support import load_extension

extension = load_extension()
config = native._encode_config(ParseLimits(max_terms=500_000_000), None, verify=True)
print("READY", flush=True)
try:
    extension._work_probe(500_000_000, config, extension._Cancellation(None))
except KeyboardInterrupt:
    raise SystemExit(73)
raise SystemExit(1)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIsNotNone(process.stdout)
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "READY")
            process.send_signal(signal.SIGINT)
            self.assertEqual(process.wait(timeout=5.0), 73)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5.0)

    @unittest.skipUnless(hasattr(os, "fork"), "fork probe requires os.fork")
    def test_immutable_runtime_self_test_is_safe_after_fork(self) -> None:
        child = os.fork()
        if child == 0:
            try:
                self.extension.self_test()
                if not native.probe().available:
                    os._exit(2)
            except BaseException:
                os._exit(1)
            os._exit(0)
        _pid, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)

    def test_runtime_policy_proactively_rejects_unsupported_modes(self) -> None:
        with patch(
            "pyowl_core.backends.native.platform.python_implementation",
            return_value="PyPy",
        ):
            self.assertIn("CPython", native._runtime_policy_reason() or "")
        with patch("pyowl_core.backends.native.sysconfig.get_config_var", return_value=1):
            self.assertIn("free-threaded", native._runtime_policy_reason() or "")
        with patch("pyowl_core.backends.native._interpreter_id", return_value=7):
            self.assertIn("subinterpreters", native._runtime_policy_reason() or "")


if __name__ == "__main__":
    unittest.main()
