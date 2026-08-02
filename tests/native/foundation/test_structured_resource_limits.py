from __future__ import annotations

from collections.abc import Callable

import pytest

from pyowl_core.backends import native
from pyowl_core.exceptions import BackendProtocolError, ResourceLimitError


class _NativeError(Exception):
    pass


class _Extension:
    _NativeError = _NativeError


def _raise(error: Exception) -> Callable[[], object]:
    def operation() -> object:
        raise error

    return operation


def _limit_error(message: str = "wording is deliberately non-contractual") -> _NativeError:
    return _NativeError(
        "NATIVE_WIRE_LIMIT",
        message,
        {
            "kind": "resource_limit",
            "limit": "max_canonical_work",
            "observed": 11,
            "allowed": 10,
            "details": {
                "component_count": 3,
                "largest_component_labels": 2,
                "largest_component_arcs": 4,
                "refinement_rounds": 1,
                "work_term": "refinement",
            },
        },
    )


def test_native_limit_payload_reaches_the_public_exception_unchanged() -> None:
    with pytest.raises(ResourceLimitError) as caught:
        native._call_parse_value(_Extension(), _raise(_limit_error()))

    error = caught.value
    assert (error.code, error.limit, error.observed, error.allowed) == (
        "NATIVE_WIRE_LIMIT",
        "max_canonical_work",
        11,
        10,
    )
    assert error.details["work_term"] == "refinement"
    assert error.as_diagnostic().details == error.details
    with pytest.raises(TypeError):
        error.details["component_count"] = 9  # type: ignore[index]


def test_limit_classification_does_not_depend_on_message_wording() -> None:
    observed = []
    for message in ("first wording", "a completely different sentence"):
        with pytest.raises(ResourceLimitError) as caught:
            native._call_index_value(_Extension(), _raise(_limit_error(message)))
        observed.append(
            (
                caught.value.limit,
                caught.value.observed,
                caught.value.allowed,
                caught.value.details,
            )
        )
    assert observed[0] == observed[1]


@pytest.mark.parametrize(
    "payload",
    (
        None,
        {
            "kind": "resource_limit",
            "limit": "max_terms",
            "observed": 2,
            "allowed": 1,
            "details": {"work_term": "message-derived"},
        },
    ),
)
def test_missing_or_malformed_native_limit_payload_fails_closed(payload: object) -> None:
    arguments = (
        ("NATIVE_WIRE_LIMIT", "legacy unstructured error")
        if payload is None
        else ("NATIVE_WIRE_LIMIT", "malformed details", payload)
    )
    with pytest.raises(BackendProtocolError) as caught:
        native._call_parse_value(_Extension(), _raise(_NativeError(*arguments)))
    assert caught.value.code == "NATIVE_LIMIT_PAYLOAD"
