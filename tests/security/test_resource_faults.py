from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pyowl_core import (
    BackendPreference,
    CancellationSource,
    DocumentFormat,
    DurabilityPolicy,
    LoadOptions,
    OperationCancelledError,
    ParseLimits,
    ResourceLimitError,
    encode_snapshot,
    parse_document,
    write_snapshot,
)
from tests.unit.wire.conftest import snapshot


def test_hostile_payload_is_absent_from_limit_diagnostics() -> None:
    secret = "BEARER_SECRET_WITH_CONTROL_\x1b_SEQUENCE"
    source = f'Ontology(AnnotationAssertion(<urn:p> <urn:s> "{secret}"))'.encode()
    with pytest.raises(ResourceLimitError) as caught:
        parse_document(
            source,
            format=DocumentFormat.FUNCTIONAL,
            options=LoadOptions(
                backend=BackendPreference.PYTHON,
                limits=ParseLimits(max_literal_bytes=8),
            ),
        )
    assert caught.value.limit == "max_literal_bytes"
    assert secret not in str(caught.value)
    assert "\x1b" not in str(caught.value)


def test_public_limit_matrix_reports_exact_named_budget() -> None:
    operations: tuple[tuple[str, Callable[[], object]], ...] = (
        (
            "max_source_bytes",
            lambda: parse_document(
                b"Ontology()",
                format="functional",
                options=LoadOptions(
                    backend=BackendPreference.PYTHON,
                    limits=ParseLimits(max_source_bytes=5),
                ),
            ),
        ),
        (
            "max_literal_bytes",
            lambda: parse_document(
                b'Ontology(AnnotationAssertion(<urn:p> <urn:s> "1234"))',
                format="functional",
                options=LoadOptions(
                    backend=BackendPreference.PYTHON,
                    limits=ParseLimits(max_literal_bytes=3),
                ),
            ),
        ),
        (
            "max_nesting_depth",
            lambda: parse_document(
                b"Ontology(SubClassOf(<urn:C> ObjectComplementOf(ObjectComplementOf(<urn:C>))))",
                format="functional",
                options=LoadOptions(
                    backend=BackendPreference.PYTHON,
                    limits=ParseLimits(max_nesting_depth=3),
                ),
            ),
        ),
        (
            "max_temporary_bytes",
            lambda: encode_snapshot(snapshot("A"), limits=ParseLimits(max_temporary_bytes=1)),
        ),
    )
    for expected, operation in operations:
        with pytest.raises(ResourceLimitError) as caught:
            operation()
        assert caught.value.limit == expected
        assert caught.value.observed is not None
        assert caught.value.allowed is not None
        assert caught.value.observed > caught.value.allowed


def test_zero_progress_write_leaves_no_partial_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "failed.pyocore"
    monkeypatch.setattr("pyowl_core.wire.cache.os.write", lambda *_args: 0)
    with pytest.raises(OSError, match="no progress"):
        write_snapshot(
            snapshot("A"),
            target,
            durability=DurabilityPolicy.NONE,
        )
    assert not target.exists()
    assert tuple(tmp_path.glob(".*.tmp")) == ()


def test_pre_cancelled_publication_leaves_no_artifact(tmp_path: Path) -> None:
    cancellation = CancellationSource()
    cancellation.cancel("fault injection")
    target = tmp_path / "cancelled.pyocore"
    with pytest.raises(OperationCancelledError) as caught:
        write_snapshot(snapshot("A"), target, cancellation_token=cancellation.token)
    assert getattr(caught.value, "code", None) == "OPERATION_CANCELLED"
    assert not target.exists()
    assert tuple(tmp_path.glob(".*.tmp")) == ()
