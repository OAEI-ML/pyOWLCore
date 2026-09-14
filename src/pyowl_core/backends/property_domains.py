"""Requested domain/range values from an owner-bound native property index."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import AxiomScope, OntologyOverlay, OntologyView
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.index.cache import IndexBuildBudget, ViewBuildStrategy, build_report
from pyowl_core.index.domains import (
    _DOMAIN_RANGE_TYPES,
    DomainRangeKind,
    DomainRangeProperty,
    DomainRangeRecord,
    NamedDomainRangeResult,
    PropertyDomainRangeOptions,
    PropertyDomainRangeView,
    _kind,
    _validate_property,
)
from pyowl_core.model import canonical_bytes, decode_canonical
from pyowl_core.model.axioms import AxiomNode

from .native_validation import _native_owner
from .native_views import _invoke_native_column_operation_v2, _selected_limits


def build_native_property_domains(
    ontology: OntologyView,
    options: PropertyDomainRangeOptions,
    budget: IndexBuildBudget,
    cancellation_token: CancellationToken | None,
    started: float,
) -> PropertyDomainRangeView:
    view = _NativePropertyDomainRangeView.__new__(_NativePropertyDomainRangeView)
    view._ontology, view.options = ontology, options
    if isinstance(ontology, OntologyOverlay):
        if options.scope is AxiomScope.CLOSURE and any(
            isinstance(axiom, _DOMAIN_RANGE_TYPES)
            for axiom in (*ontology.delta.add_axioms, *ontology.delta.remove_axioms)
        ):
            raise BackendProtocolError(
                "native domain/range changes in overlays are unsupported",
                code="NATIVE_VIEW_REQUIRED",
            )
        inherited = ontology.base.view(
            PropertyDomainRangeView,
            scope=options.scope,
            document_key=options.document_key,
            include_origins=options.include_origins,
            require_native_pipeline=True,
            cancellation_token=cancellation_token,
        )
        if not isinstance(inherited, _NativePropertyDomainRangeView):
            raise BackendProtocolError(
                "native domain/range index unavailable", code="NATIVE_VIEW_REQUIRED"
            )
        view._native_index, view._native_extension = (
            inherited._native_index,
            inherited._native_extension,
        )
        budget.add_shared_rows(inherited.report.total_row_count)
        view.report = build_report(
            PropertyDomainRangeView,
            ViewBuildStrategy.PATCHED,
            budget,
            started,
            shared_bytes=inherited.report.own_bytes + inherited.report.shared_bytes,
        )
        return view
    selected = _native_owner(ontology)
    if selected is None or getattr(selected[1], "NATIVE_PROPERTY_DOMAINS_API_VERSION", None) != 1:
        raise BackendProtocolError(
            "native property domain/range index unavailable for this owner",
            code="NATIVE_VIEW_REQUIRED",
        )
    raw, extension = selected
    scope, ordinal = cast(Any, ontology)._native_scope(options.scope, options.document_key)
    index = _invoke_native_column_operation_v2(
        extension,
        extension._property_domains_v1,
        (raw, scope.value, ordinal),
        _selected_limits(ontology, None),
        cancellation_token,
    )
    observed = cast(Any, index)._report_v1()
    budget.add(
        "native_property_domains",
        rows=observed["selected_axioms"],
        bytes_=observed["allocation_charge_bytes"],
    )
    view._native_index, view._native_extension = index, extension
    view.report = build_report(
        PropertyDomainRangeView, ViewBuildStrategy.FULL_BUILD, budget, started
    )
    return view


class _NativePropertyDomainRangeView(PropertyDomainRangeView):
    _native_index: Any
    _native_extension: Any

    def _call(self, name: str, *args: Any) -> Any:
        cast(Any, self._ontology)._check_open()
        return _invoke_native_column_operation_v2(
            self._native_extension,
            getattr(self._native_index, name),
            args,
            _selected_limits(self._ontology, None),
            None,
        )

    @property
    def native_report(self) -> Mapping[str, object]:
        cast(Any, self._ontology)._check_open()
        return MappingProxyType(self._native_index._report_v1())

    def _records(
        self, key: bytes | None, kind: DomainRangeKind | None, named_only: bool, limit: int | None
    ) -> Iterator[tuple[bytes, bytes]]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a nonnegative integer or None")
        cursor, emitted = 0, 0
        while limit is None or emitted < limit:
            size = 64 if limit is None else min(64, limit - emitted)
            cursor, done, rows = self._call(
                "_records_v1",
                key,
                None if kind is None else kind is DomainRangeKind.RANGE,
                named_only,
                cursor,
                size,
            )
            yield from rows
            emitted += len(rows)
            if done:
                return

    def _record(self, encoded: bytes, digest: bytes) -> DomainRangeRecord:
        axiom = cast(AxiomNode, decode_canonical(encoded))
        origins: Any = ()
        if self.options.include_origins:
            if isinstance(self._ontology, OntologyOverlay):
                origins = self._ontology.origins_for(axiom)
            else:
                origins = self._ontology.origin_index.entries.get(digest, ())
        if hasattr(axiom, "domain"):
            kind, value = DomainRangeKind.DOMAIN, cast(Any, axiom).domain
        else:
            kind, value = DomainRangeKind.RANGE, cast(Any, axiom).range
        return DomainRangeRecord(cast(Any, axiom).property, kind, value, axiom, origins)

    def iter(
        self,
        property: DomainRangeProperty,
        kind: DomainRangeKind | str | None = None,
        *,
        named_only: bool = False,
        limit: int | None = None,
    ) -> Iterator[DomainRangeRecord]:
        _validate_property(property)
        selected = _kind(kind)
        if not isinstance(named_only, bool):
            raise TypeError("named_only must be bool")
        for encoded, digest in self._records(
            canonical_bytes(property), selected, named_only, limit
        ):
            yield self._record(encoded, digest)

    def named(
        self, property: DomainRangeProperty, kind: DomainRangeKind | str
    ) -> NamedDomainRangeResult:
        _validate_property(property)
        selected = _kind(kind)
        _, filtered = self._call(
            "_counts_v1",
            canonical_bytes(property),
            None if selected is None else selected is DomainRangeKind.RANGE,
        )
        return NamedDomainRangeResult(
            tuple(self.iter(property, selected, named_only=True)), filtered
        )

    def properties(self, *, limit: int | None = None) -> Iterator[DomainRangeProperty]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a nonnegative integer or None")
        after, emitted = None, 0
        while limit is None or emitted < limit:
            size = 64 if limit is None else min(64, limit - emitted)
            rows = self._call("_properties_v1", after, size)
            for encoded in rows:
                yield cast(DomainRangeProperty, decode_canonical(encoded))
            if len(rows) < size:
                return
            emitted += len(rows)
            after = rows[-1]

    def _iter_axioms(self) -> Iterator[AxiomNode]:
        for encoded, _ in self._records(None, None, False, None):
            yield cast(AxiomNode, decode_canonical(encoded))
