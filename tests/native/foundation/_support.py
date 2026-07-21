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
    INGESTION_FEATURES: tuple[str, ...]
    VIEW_FEATURES: tuple[str, ...]
    _NativeError: type[Exception]
    _Cancellation: Callable[[float | None], NativeTestCancellation]

    def self_test(self) -> None: ...

    def _panic_probe(self) -> None: ...

    def _work_probe(self, iterations: int, config: object, cancel: object) -> int: ...

    def _component_roundtrip_v1(
        self, canonical: object, config: object, cancel: object | None = None
    ) -> bytes: ...

    def _component_allocation_probe_v1(
        self,
        canonical: object,
        config: object,
        phase: str,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...

    def _wire_allocation_probe_v1(
        self,
        snapshot_wire: object,
        config: object,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...

    def _parser_allocation_probe_v1(
        self,
        source: object,
        config: object,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...

    def _parser_bridge_allocation_probe_v1(
        self,
        source: object,
        config: object,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...

    def _functional_retained_bridge_allocation_probe_v2(
        self,
        source: object,
        config: object,
        collect_provenance: bool,
        preserve_source_map: bool,
        record_unresolved: bool,
        require_empty_imports: bool,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...

    def _rdfxml_retained_bridge_allocation_probe_v2(
        self,
        source: object,
        document_iri: object | None,
        config: object,
        collect_provenance: bool,
        allow_partial_rdf_mapping: bool,
        require_empty_imports: bool,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...

    def _index_bridge_allocation_probe_v1(
        self,
        snapshot_wire: object,
        request: object,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...

    def _foundation_bridge_allocation_probe_v1(
        self,
        operation: str,
        source: object,
        config: object,
        fail_after: int | None = None,
    ) -> tuple[bytes, int]: ...


def load_extension() -> NativeTestExtension:
    """Load an installed extension or an explicitly supplied developer build."""

    selected = os.environ.get("PYOWL_CORE_TEST_NATIVE_LIBRARY")
    if selected and selected != "1":
        path = Path(selected).resolve()
        if not path.is_file():
            raise unittest_skip("PYOWL_CORE_TEST_NATIVE_LIBRARY does not name a file")
        return _load_extension_path(path)
    retained = sys.modules.get("pyowl_core._native")
    if isinstance(retained, ModuleType):
        return cast(NativeTestExtension, retained)
    try:
        return cast(NativeTestExtension, importlib.import_module("pyowl_core._native"))
    except ImportError:
        if not selected:
            raise unittest_skip("native extension is not installed in this test lane") from None
        raise unittest_skip("native extension is not installed in this test lane") from None


def _load_extension_path(path: Path) -> NativeTestExtension:
    name = "pyowl_core._native"
    retained = sys.modules.pop(name, None)
    loader = importlib.machinery.ExtensionFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        if retained is not None:
            sys.modules[name] = retained
        raise unittest_skip("developer native library cannot be loaded")
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        if retained is not None:
            sys.modules[name] = retained
        raise
    return cast(NativeTestExtension, module)


def unittest_skip(message: str) -> BaseException:
    from unittest import SkipTest

    return SkipTest(message)


__all__ = ["NativeTestExtension", "load_extension"]
