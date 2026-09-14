"""Native selected annotation pages; Python wraps requested scalar values only."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import OntologyView
from pyowl_core.document.provenance import OriginOccurrence
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.index.cache import IndexBuildBudget
from pyowl_core.model import (
    IRI,
    AnnotationProperty,
    AnnotationSubject,
    AnnotationValue,
    AnonymousIndividual,
    Literal,
    StructuralNode,
    canonical_bytes,
    decode_canonical,
)

from .native_validation import _native_owner
from .native_views import _invoke_native_column_operation_v2, _selected_limits


@dataclass(frozen=True, slots=True)
class AnnotationAssertionColumns:
    """Immutable canonical assertion-order columns, retaining complete axiom identity.

    Published bytes and scalar values remain valid after the source closes. The
    index follows its owner's existing close contract for subsequent requests.
    Equal subject/property/value triples with different annotations remain rows.
    """

    subjects: tuple[AnnotationSubject, ...]
    properties: tuple[AnnotationProperty, ...]
    values: tuple[AnnotationValue, ...]
    canonical_assertion_bytes: tuple[bytes, ...]
    assertion_digests: tuple[bytes, ...]
    origins: tuple[tuple[OriginOccurrence, ...], ...]
    report: Mapping[str, object]


def supports_native_columns(ontology: object, *, include_nested: bool = False) -> bool:
    if include_nested:
        return False
    selected = _native_owner(cast(OntologyView, ontology))
    return (
        selected is not None
        and getattr(selected[1], "NATIVE_ANNOTATION_COLUMNS_API_VERSION", None) == 1
    )


def build_native_columns(
    ontology: OntologyView,
    options: Any,
    budget: IndexBuildBudget,
    cancellation_token: CancellationToken | None,
) -> Any:
    if not supports_native_columns(ontology, include_nested=options.include_nested):
        raise BackendProtocolError(
            "native annotation columns unavailable for this owner/options",
            code="NATIVE_VIEW_REQUIRED",
        )
    selected = _native_owner(ontology)
    assert selected is not None
    raw, extension = selected
    scope, ordinal = cast(Any, ontology)._native_scope(options.scope, options.document_key)
    result = _invoke_native_column_operation_v2(
        extension,
        extension._annotation_columns_v1,
        (raw, scope.value, ordinal),
        _selected_limits(ontology, None),
        cancellation_token,
    )
    report = cast(Any, result)._report_v1()
    budget.add(
        "native_annotation_roots", rows=report["annotation_rows"], bytes_=report["retained_bytes"]
    )
    return result


def iter_native_columns(
    index: Any,
    *,
    subjects: Iterable[AnnotationSubject] | None,
    properties: Iterable[AnnotationProperty] | None,
    max_rows: int,
    max_bytes: int,
    cancellation_token: CancellationToken | None,
) -> Iterator[AnnotationAssertionColumns]:
    for name, value in (("max_rows", max_rows), ("max_bytes", max_bytes)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    ontology = index._ontology
    cast(Any, ontology)._check_open()
    selected = _native_owner(ontology)
    if selected is None or index._native_columns is None:
        raise BackendProtocolError(
            "index was not created with require_native_pipeline=True", code="NATIVE_VIEW_REQUIRED"
        )
    _raw, extension = selected
    limits = _selected_limits(ontology, None)

    def filters(values: Iterable[Any] | None, kinds: tuple[type, ...]) -> list[bytes] | None:
        if values is None:
            return None
        result: list[bytes] = []
        retained_bytes = 0
        for value in values:
            if not isinstance(value, kinds):
                raise TypeError("annotation filters must use existing annotation identity types")
            limits.enforce("max_index_rows", len(result) + 1)
            encoded = canonical_bytes(cast(StructuralNode, value))
            retained_bytes += len(encoded) + 64
            limits.enforce("max_index_bytes", retained_bytes)
            result.append(encoded)
        return result

    subject_keys = filters(subjects, (IRI, AnonymousIndividual))
    property_keys = filters(properties, (AnnotationProperty,))
    if subject_keys == [] or property_keys == []:
        return
    cursor = 0
    native_report = index._native_columns._report_v1()
    total = native_report["annotation_rows"]
    while cursor < total:
        cast(Any, ontology)._check_open()
        raw_page = _invoke_native_column_operation_v2(
            extension,
            index._native_columns._page_v1,
            (cursor, subject_keys, property_keys, max_rows, max_bytes),
            limits,
            cancellation_token,
        )
        rows, cursor, scanned, payload_bytes = cast(Any, raw_page)
        if not rows:
            continue
        subject_values = tuple(cast(AnnotationSubject, decode_canonical(row[0])) for row in rows)
        property_values = tuple(cast(AnnotationProperty, decode_canonical(row[1])) for row in rows)
        values = tuple(cast(AnnotationValue, decode_canonical(row[2])) for row in rows)
        if any(not isinstance(value, (IRI, AnonymousIndividual, Literal)) for value in values):
            raise BackendProtocolError(
                "invalid native annotation value", code="NATIVE_VIEW_REQUIRED"
            )
        digests = tuple(row[4] for row in rows)
        origins = (
            tuple(ontology.origin_index.entries.get(digest, ()) for digest in digests)
            if index.options.include_origins
            else tuple(() for _ in rows)
        )
        yield AnnotationAssertionColumns(
            subject_values,
            property_values,
            values,
            tuple(row[3] for row in rows),
            digests,
            origins,
            MappingProxyType(
                {
                    "backend": "native",
                    "scanned_annotation_rows": scanned,
                    "published_rows": len(rows),
                    "published_payload_bytes": payload_bytes,
                    "index_build_scanned_roots": native_report["scanned_roots"],
                    "index_retained_bytes": native_report["retained_bytes"],
                }
            ),
        )
