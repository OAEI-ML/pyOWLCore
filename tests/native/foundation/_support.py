from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast


class NativeTestCancellation(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def cancel(self) -> bool: ...


class NativeTestExtension(Protocol):
    ABI_VERSION: int
    MODEL_SCHEMA_VERSION: int
    WIRE_FORMAT_VERSION: tuple[int, int]
    FEATURES: tuple[str, ...]
    _NativeError: type[Exception]
    _Cancellation: Callable[[float | None], NativeTestCancellation]

    def self_test(self) -> None: ...

    def _panic_probe(self) -> None: ...

    def _work_probe(self, iterations: int, config: object, cancel: object) -> int: ...


def load_extension() -> NativeTestExtension:
    """Load an installed extension or an explicitly supplied developer build."""

    retained = sys.modules.get("pyowl_core._native")
    if isinstance(retained, ModuleType):
        return cast(NativeTestExtension, retained)
    try:
        return cast(NativeTestExtension, importlib.import_module("pyowl_core._native"))
    except ImportError:
        selected = os.environ.get("PYOWL_CORE_TEST_NATIVE_LIBRARY")
        if not selected:
            raise unittest_skip("native extension is not installed in this test lane") from None
        path = Path(selected).resolve()
        if not path.is_file():
            raise unittest_skip("PYOWL_CORE_TEST_NATIVE_LIBRARY does not name a file") from None
        name = "pyowl_core._native"
        loader = importlib.machinery.ExtensionFileLoader(name, str(path))
        spec = importlib.util.spec_from_loader(name, loader)
        if spec is None:
            raise unittest_skip("developer native library cannot be loaded") from None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        return cast(NativeTestExtension, module)


def unittest_skip(message: str) -> BaseException:
    from unittest import SkipTest

    return SkipTest(message)


__all__ = ["NativeTestExtension", "load_extension"]
