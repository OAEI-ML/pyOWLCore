"""Public admission of native-issued, immutable structural publications.

This capability initially covers retained native snapshots. Unrecognized and
transformed owners are rejected before the scalar producer in strict requests.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn, cast

from pyowl_core.document import AxiomScope, OntologyView
from pyowl_core.document.document import Fingerprint
from pyowl_core.exceptions import (
    BackendProtocolError,
    ClosedSnapshotError,
    OperationCancelledError,
    ResourceLimitError,
)
from pyowl_core.limits import ParseLimits

if TYPE_CHECKING:
    from .native_views import EncodedStructuralViewV2


def native_validation_available() -> bool:
    """Probe the installed binary capability without loading an ontology.

    A positive probe never substitutes for owner-bound receipt admission.
    """
    try:
        extension = importlib.import_module("pyowl_core._native")
    except (ImportError, OSError):
        return False
    version = getattr(extension, "NATIVE_VALIDATED_COLUMNS_API_VERSION", None)
    return (
        type(version) is int
        and version == 1
        and isinstance(getattr(extension, "NativeColumnValidationReceiptV1", None), type)
        and callable(getattr(extension, "_validated_encoded_structural_columns_v2", None))
    )


def _native_owner(owner: OntologyView) -> tuple[Any, Any] | None:
    from pyowl_core.document.native_storage import _NativeOntologySnapshot

    if type(owner) is not _NativeOntologySnapshot:
        return None
    raw = object.__getattribute__(owner._native_snapshot_state.owner.handle, "_owner_v2")
    return raw, importlib.import_module(type(raw).__module__)


def encoded_scopes_equivalent(
    owner: OntologyView,
    left_scope: AxiomScope,
    right_scope: AxiomScope,
    *,
    left_document_key: str | None = None,
    right_document_key: str | None = None,
) -> bool:
    """Prove equivalent retained selections, without producing encoded columns.

    False means no proof, not necessarily different contents. Currently only an
    untransformed, one-document native snapshot without import edges is admitted.
    """
    from .native_views import _validate_selection

    _validate_selection(left_scope, left_document_key)
    _validate_selection(right_scope, right_document_key)
    selected = _native_owner(owner)
    if selected is None:
        return False
    raw, extension = selected
    operation = getattr(extension, "_encoded_scopes_equivalent_v1", None)
    if not callable(operation):
        return False
    left, left_ordinal = cast(Any, owner)._native_scope(left_scope, left_document_key)
    right, right_ordinal = cast(Any, owner)._native_scope(right_scope, right_document_key)
    return bool(operation(raw, left.value, left_ordinal, right.value, right_ordinal))


def validate_native_columns(
    candidate: object,
    *,
    expected_owner: OntologyView,
    expected_scope: AxiomScope,
    expected_document_key: str | None,
    limits: ParseLimits | None,
) -> EncodedStructuralViewV2:
    from . import native
    from .native_views import (
        _POSTINGS_ALL,
        _SEGMENT_DIRECT,
        ENCODED_STRUCTURAL_DESCRIPTOR_V2,
        ENCODED_STRUCTURAL_MODEL_SCHEMA_V2,
        ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
        ENCODED_STRUCTURAL_SCHEMA_VERSION_V2,
        EncodedStructuralViewV2,
        _selected_limits,
        _validate_selection,
        produce_encoded_structural_view_v2,
    )

    started = monotonic()

    def reject() -> NoReturn:
        raise BackendProtocolError(
            "publication lacks matching native validation", code="NATIVE_VIEW_REQUIRED"
        )

    _validate_selection(expected_scope, expected_document_key)
    if type(candidate) is not EncodedStructuralViewV2:
        return reject()
    selected = _native_owner(expected_owner)
    if selected is None:
        return reject()
    _raw, extension = selected
    receipt = cast(Any, candidate._native_receipt)
    receipt_type = getattr(extension, "NativeColumnValidationReceiptV1", None)
    if receipt_type is None or type(receipt) is not receipt_type:
        return reject()
    if (
        candidate.owner is not expected_owner
        or candidate.scope is not expected_scope
        or candidate.document_key != expected_document_key
        or candidate.schema_name != ENCODED_STRUCTURAL_SCHEMA_NAME_V2
        or candidate.schema_version != ENCODED_STRUCTURAL_SCHEMA_VERSION_V2
        or candidate.model_schema != ENCODED_STRUCTURAL_MODEL_SCHEMA_V2
        or candidate.descriptor != ENCODED_STRUCTURAL_DESCRIPTOR_V2
        or len(candidate.segments) != 1
    ):
        return reject()
    segment = candidate.segments[0]
    if (
        segment.role != _SEGMENT_DIRECT
        or segment.owner is not expected_owner
        or segment.source is not None
        or segment.posting_mode != _POSTINGS_ALL
        or len(segment.root_ids)
        or len(segment.anonymous_scope_map)
        or segment.member_token is not None
    ):
        return reject()
    scope, ordinal = cast(Any, expected_owner)._native_scope(expected_scope, expected_document_key)
    try:
        matched = receipt._matches_v1(expected_owner, scope.value, ordinal, dict(candidate.buffers))
    except (ClosedSnapshotError, ResourceLimitError):
        raise
    except Exception as error:
        raise BackendProtocolError(
            "invalid native column receipt", code="NATIVE_VIEW_REQUIRED"
        ) from error
    if not matched:
        return reject()
    if candidate.structural_fingerprint != Fingerprint("sha256", 2, receipt._fingerprint_v1()):
        return reject()
    selected_limits = _selected_limits(expected_owner, limits)
    config = native._encode_config(selected_limits, None, verify=False)
    if not receipt._same_limits_v1(config):
        # Recheck native construction under changed budgets, not a weaker receipt.
        return produce_encoded_structural_view_v2(
            expected_owner,
            scope=expected_scope,
            document_key=expected_document_key,
            limits=selected_limits,
            require_native_validation=True,
        )
    if (
        selected_limits.deadline_seconds is not None
        and monotonic() - started > selected_limits.deadline_seconds
    ):
        raise OperationCancelledError("operation deadline exceeded", reason="deadline exceeded")
    return candidate


def native_validation_report(view: object) -> Mapping[str, object]:
    """Return native construction diagnostics after owner-bound admission."""
    from .native_views import EncodedStructuralViewV2

    if type(view) is not EncodedStructuralViewV2:
        raise TypeError("view must be EncodedStructuralViewV2")
    checked = validate_native_columns(
        view,
        expected_owner=view.owner,
        expected_scope=view.scope,
        expected_document_key=view.document_key,
        limits=view._native_limits,
    )
    return MappingProxyType(
        cast(dict[str, object], cast(Any, checked._native_receipt)._report_v1())
    )
