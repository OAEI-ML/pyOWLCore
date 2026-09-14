"""Compact retained constructor/entity queries; wrap only requested axiom rows."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import AxiomScope, OntologyOverlay, OntologyView
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.index.axiom_types import (
    _REGISTRY_AXIOM_CONSTRUCTOR_TAGS,
    CATEGORY_TYPES,
    A,
    AxiomCategory,
    AxiomPosting,
    AxiomTypeIndex,
    AxiomTypeOptions,
    _category,
)
from pyowl_core.index.cache import IndexBuildBudget, ViewBuildStrategy, build_report
from pyowl_core.index.common import validate_axiom_type
from pyowl_core.model import Entity, canonical_bytes, decode_canonical
from pyowl_core.model.axioms import ANNOTATION_AXIOM_TYPES, AXIOM_TYPES, AxiomNode

from .native_validation import _native_owner
from .native_views import _invoke_native_column_operation_v2, _selected_limits


def build_native_axiom_index(
    ontology: OntologyView,
    options: AxiomTypeOptions,
    budget: IndexBuildBudget,
    cancellation_token: CancellationToken | None,
    started: float,
) -> AxiomTypeIndex:
    view = _NativeAxiomTypeIndex.__new__(_NativeAxiomTypeIndex)
    view._ontology = ontology
    view.options = options
    view._annotation_overlay = False
    if isinstance(ontology, OntologyOverlay):
        if options.scope is AxiomScope.CLOSURE and any(
            type(row) not in ANNOTATION_AXIOM_TYPES
            for row in (*ontology.delta.add_axioms, *ontology.delta.remove_axioms)
        ):
            raise BackendProtocolError(
                "native axiom index changes in overlays are unsupported",
                code="NATIVE_VIEW_REQUIRED",
            )
        inherited = ontology.base.view(
            AxiomTypeIndex,
            scope=options.scope,
            document_key=options.document_key,
            include_origins=options.include_origins,
            require_native_pipeline=True,
            cancellation_token=cancellation_token,
        )
        if not isinstance(inherited, _NativeAxiomTypeIndex):
            raise BackendProtocolError(
                "native typed index unavailable", code="NATIVE_VIEW_REQUIRED"
            )
        view._index = inherited._index
        view._extension = inherited._extension
        view._annotation_overlay = inherited._annotation_overlay or (
            options.scope is AxiomScope.CLOSURE
            and bool(ontology.delta.add_axioms or ontology.delta.remove_axioms)
        )
        budget.add_shared_rows(inherited.report.total_row_count)
        view.report = build_report(
            AxiomTypeIndex,
            ViewBuildStrategy.PATCHED,
            budget,
            started,
            shared_bytes=inherited.report.own_bytes + inherited.report.shared_bytes,
        )
        return view
    selected = _native_owner(ontology)
    if selected is None or getattr(selected[1], "NATIVE_AXIOM_INDEX_API_VERSION", None) != 1:
        raise BackendProtocolError(
            "native compact axiom index unavailable for this owner", code="NATIVE_VIEW_REQUIRED"
        )
    raw, extension = selected
    scope, ordinal = cast(Any, ontology)._native_scope(options.scope, options.document_key)
    index = _invoke_native_column_operation_v2(
        extension,
        extension._axiom_index_v1,
        (raw, scope.value, ordinal),
        _selected_limits(ontology, None),
        cancellation_token,
    )
    report = cast(Any, index)._report_v1()
    budget.add(
        "native_axiom_index", rows=report["scanned_roots"], bytes_=report["allocation_charge_bytes"]
    )
    view._index = index
    view._extension = extension
    view.report = build_report(AxiomTypeIndex, ViewBuildStrategy.FULL_BUILD, budget, started)
    return view


class _NativeAxiomTypeIndex(AxiomTypeIndex):
    _index: Any
    _extension: Any
    _annotation_overlay: bool

    @property
    def native_report(self) -> Mapping[str, object]:
        cast(Any, self._ontology)._check_open()
        return MappingProxyType(self._index._report_v1())

    def _tags(self, constructors: tuple[type[AxiomNode], ...]) -> list[int]:
        cast(Any, self._ontology)._check_open()
        if self._annotation_overlay and any(
            constructor in ANNOTATION_AXIOM_TYPES for constructor in constructors
        ):
            raise BackendProtocolError(
                "native typed annotation overlay queries unavailable; use AnnotationAssertionIndex",
                code="NATIVE_VIEW_REQUIRED",
            )
        return [_REGISTRY_AXIOM_CONSTRUCTOR_TAGS[constructor] for constructor in constructors]

    def _rows(
        self,
        constructors: tuple[type[AxiomNode], ...],
        referencing: Entity | None,
        limit: int | None,
    ) -> Iterator[AxiomNode]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a nonnegative integer or None")
        if referencing is not None and not isinstance(referencing, Entity):
            raise TypeError("referencing must be an Entity or None")
        tags = self._tags(constructors)
        entity = None if referencing is None else canonical_bytes(referencing)
        cursor = 0
        yielded = 0
        while limit is None or yielded < limit:
            cast(Any, self._ontology)._check_open()
            count = 64 if limit is None else min(64, limit - yielded)
            result = _invoke_native_column_operation_v2(
                self._extension,
                self._index._page_v1,
                (tags, entity, cursor, count),
                _selected_limits(self._ontology, None),
                None,
            )
            rows, cursor = cast(Any, result)
            for row in rows:
                yield cast(AxiomNode, decode_canonical(row))
            yielded += len(rows)
            if len(rows) < count:
                return

    def iter(
        self, axiom_type: type[A], *, referencing: Entity | None = None, limit: int | None = None
    ) -> Iterator[A]:
        constructor = validate_axiom_type(axiom_type)
        yield from cast(Iterator[A], self._rows((constructor,), referencing, limit))

    def iter_category(
        self, category: AxiomCategory | str | object, *, limit: int | None = None
    ) -> Iterator[AxiomNode]:
        yield from self._rows(CATEGORY_TYPES[_category(category)], None, limit)

    def iter_all(self, *, limit: int | None = None) -> Iterator[AxiomNode]:
        yield from self._rows(AXIOM_TYPES, None, limit)

    def count(self, axiom_type: type[A], *, referencing: Entity | None = None) -> int:
        if referencing is not None and not isinstance(referencing, Entity):
            raise TypeError("referencing must be an Entity or None")
        tags = self._tags((validate_axiom_type(axiom_type),))
        return cast(
            int,
            self._index._count_v1(
                tags, None if referencing is None else canonical_bytes(referencing)
            ),
        )

    def count_category(self, category: AxiomCategory | str | object) -> int:
        return cast(
            int, self._index._count_v1(self._tags(CATEGORY_TYPES[_category(category)]), None)
        )

    def posting(self, axiom: AxiomNode) -> AxiomPosting | None:
        # The request identifies one axiom; membership is checked natively.
        tags = self._tags((validate_axiom_type(type(axiom)),))
        target = canonical_bytes(axiom)
        result = _invoke_native_column_operation_v2(
            self._extension,
            self._index._contains_v1,
            (tags[0], target),
            _selected_limits(self._ontology, None),
            None,
        )
        if not result:
            return None
        owner = self._ontology
        while isinstance(owner, OntologyOverlay):
            owner = owner.base
        from pyowl_core.model import structural_digest

        origins = (
            owner.origin_index.entries.get(structural_digest(axiom), ())
            if self.options.include_origins
            else ()
        )
        return AxiomPosting(axiom, origins)
