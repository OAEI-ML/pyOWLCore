"""Executable WP14 comparator pipeline.

Exports are resolved lazily so running a submodule with ``python -m`` does not
pre-import that submodule through the package initializer.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "build_core_common_contract": (".common_contract", "build_core_common_contract"),
    "check_comparator_contract": (".runner", "check_comparator_contract"),
    "load_comparator_manifest": (".manifest", "load_comparator_manifest"),
    "run_comparator_baseline": (".runner", "run_comparator_baseline"),
    "run_core_adapter": (".adapters", "run_core_adapter"),
    "run_external_adapter": (".adapters", "run_external_adapter"),
    "validate_common_contract": (".common_contract", "validate_common_contract"),
}

__all__ = tuple(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
