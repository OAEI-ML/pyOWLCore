from __future__ import annotations

from collections.abc import Iterator

import pytest

from pyowl_core.adapters import OperationCounters
from pyowl_core.backends.python import PythonParser


@pytest.fixture
def operation_counters(monkeypatch: pytest.MonkeyPatch) -> Iterator[OperationCounters]:
    """Count every root/import parser dispatch, including forced native dispatch."""

    counters = OperationCounters()
    original = PythonParser.parse

    def counted(self: PythonParser, *args: object, **kwargs: object) -> object:
        counters.increment("parser")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(PythonParser, "parse", counted)
    yield counters
