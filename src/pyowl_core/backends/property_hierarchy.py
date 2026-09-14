"""Requested property hierarchy wrappers over the shared native index engine."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from functools import partial
from types import MappingProxyType
from typing import Any, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import AxiomScope, OntologyOverlay, OntologyView
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.index.cache import IndexBuildBudget, ViewBuildStrategy, build_report
from pyowl_core.index.hierarchy import (
    _PROPERTY_AXIOM_TYPES,
    AssertedPropertyHierarchyView,
    InversePropertyRecord,
    PropertyChainRecord,
    PropertyComponent,
    PropertyEquivalenceRecord,
    PropertyHierarchyEdge,
    PropertyHierarchyNode,
    PropertyHierarchyOptions,
)
from pyowl_core.model import DataProperty, ObjectProperty, canonical_bytes, decode_canonical
from pyowl_core.model.axioms import (
    AxiomNode,
    EquivalentDataProperties,
    EquivalentObjectProperties,
    InverseObjectProperties,
    SubObjectPropertyOf,
)

from .class_hierarchy import _NativeClassHierarchyView
from .native_validation import _native_owner
from .native_views import _invoke_native_column_operation_v2, _selected_limits


def build_native_property_hierarchy(
    ontology: OntologyView,
    options: PropertyHierarchyOptions,
    budget: IndexBuildBudget,
    cancellation_token: CancellationToken | None,
    started: float,
) -> AssertedPropertyHierarchyView:
    view = _NativePropertyHierarchyView.__new__(_NativePropertyHierarchyView)
    view._ontology = ontology
    view.options = options
    if isinstance(ontology, OntologyOverlay):
        if options.scope is AxiomScope.CLOSURE and any(
            isinstance(row, _PROPERTY_AXIOM_TYPES)
            for row in (*ontology.delta.add_axioms, *ontology.delta.remove_axioms)
        ):
            raise BackendProtocolError(
                "native property hierarchy changes in overlays are unsupported",
                code="NATIVE_VIEW_REQUIRED",
            )
        inherited = ontology.base.view(
            AssertedPropertyHierarchyView,
            scope=options.scope,
            document_key=options.document_key,
            include_origins=options.include_origins,
            equivalence_handling=options.equivalence_handling,
            require_native_pipeline=True,
            cancellation_token=cancellation_token,
        )
        if not isinstance(inherited, _NativePropertyHierarchyView):
            raise BackendProtocolError(
                "native property hierarchy unavailable", code="NATIVE_VIEW_REQUIRED"
            )
        view._native_index = inherited._native_index
        view._native_extension = inherited._native_extension
        budget.add_shared_rows(inherited.report.total_row_count)
        view.report = build_report(
            AssertedPropertyHierarchyView,
            ViewBuildStrategy.PATCHED,
            budget,
            started,
            shared_bytes=inherited.report.own_bytes + inherited.report.shared_bytes,
        )
        return view
    selected = _native_owner(ontology)
    if selected is None or getattr(selected[1], "NATIVE_PROPERTY_HIERARCHY_API_VERSION", None) != 1:
        raise BackendProtocolError(
            "native asserted property hierarchy unavailable for this owner",
            code="NATIVE_VIEW_REQUIRED",
        )
    raw, extension = selected
    scope, ordinal = cast(Any, ontology)._native_scope(options.scope, options.document_key)
    index = _invoke_native_column_operation_v2(
        extension,
        partial(extension._class_hierarchy_v1, property_mode=True),
        (raw, scope.value, ordinal, options.equivalence_handling.value, False),
        _selected_limits(ontology, None),
        cancellation_token,
    )
    native_report = cast(Any, index)._report_v1()
    budget.add(
        "native_property_hierarchy",
        rows=native_report["selected_axioms"],
        bytes_=native_report["allocation_charge_bytes"],
    )
    view._native_index = index
    view._native_extension = extension
    view.report = build_report(
        AssertedPropertyHierarchyView, ViewBuildStrategy.FULL_BUILD, budget, started
    )
    return view


class _NativePropertyHierarchyView(AssertedPropertyHierarchyView):
    _native_index: Any
    _native_extension: Any

    @property
    def native_report(self) -> Mapping[str, object]:
        cast(Any, self._ontology)._check_open()
        return MappingProxyType(self._native_index._report_v1())

    def _records(self, kind: str, limit: int | None) -> Iterator[Any]:
        yield from _NativeClassHierarchyView._records(cast(Any, self), kind, limit)

    def _origins(self, digest: bytes) -> Any:
        owner = self._ontology
        while isinstance(owner, OntologyOverlay):
            owner = owner.base
        return owner.origin_index.entries.get(digest, ()) if self.options.include_origins else ()

    def _node(self, row: Any) -> PropertyHierarchyNode:
        members, component = row
        properties = tuple(
            cast(ObjectProperty | DataProperty, decode_canonical(value)) for value in members
        )
        return PropertyComponent(properties) if component else properties[0]

    def _nodes(self, kind: str, value: PropertyHierarchyNode) -> Iterator[PropertyHierarchyNode]:
        cast(Any, self._ontology)._check_open()
        if not isinstance(value, (ObjectProperty, DataProperty, PropertyComponent)):
            raise TypeError("value must be a named property or PropertyComponent")
        members = value.members if isinstance(value, PropertyComponent) else (value,)
        rows = _invoke_native_column_operation_v2(
            self._native_extension,
            self._native_index._nodes_v1,
            (
                kind,
                [canonical_bytes(item) for item in members],
                isinstance(value, PropertyComponent),
            ),
            _selected_limits(self._ontology, None),
            None,
        )
        yield from (self._node(row) for row in cast(Any, rows))

    def iter_edges(self, *, limit: int | None = None) -> Iterator[PropertyHierarchyEdge]:
        for child, parent, axiom, digest in self._records("edges", limit):
            yield PropertyHierarchyEdge(
                self._node(child),
                self._node(parent),
                cast(AxiomNode, decode_canonical(axiom)),
                self._origins(digest),
            )

    def equivalence_sets(self, *, limit: int | None = None) -> Iterator[PropertyEquivalenceRecord]:
        for members, axiom, digest in self._records("equivalences", limit):
            yield PropertyEquivalenceRecord(
                tuple(
                    cast(ObjectProperty | DataProperty, decode_canonical(item)) for item in members
                ),
                cast(
                    EquivalentObjectProperties | EquivalentDataProperties, decode_canonical(axiom)
                ),
                self._origins(digest),
            )

    def inverses(self, *, limit: int | None = None) -> Iterator[InversePropertyRecord]:
        for data, digest in self._records("inverses", limit):
            axiom = cast(InverseObjectProperties, decode_canonical(data))
            yield InversePropertyRecord(axiom.first, axiom.second, axiom, self._origins(digest))

    def chains(self, *, limit: int | None = None) -> Iterator[PropertyChainRecord]:
        from pyowl_core.model import ObjectPropertyChain

        for data, digest in self._records("chains", limit):
            axiom = cast(SubObjectPropertyOf, decode_canonical(data))
            yield PropertyChainRecord(
                cast(ObjectPropertyChain, axiom.sub_property),
                axiom.super_property,
                axiom,
                self._origins(digest),
            )

    def asserted_parents(self, value: PropertyHierarchyNode) -> Iterator[PropertyHierarchyNode]:
        yield from self._nodes("parents", value)

    def asserted_children(self, value: PropertyHierarchyNode) -> Iterator[PropertyHierarchyNode]:
        yield from self._nodes("children", value)

    def equivalents(
        self, value: ObjectProperty | DataProperty
    ) -> Iterator[ObjectProperty | DataProperty]:
        if not isinstance(value, (ObjectProperty, DataProperty)):
            raise TypeError("value must be a named object/data property")
        yield from cast(Iterator[ObjectProperty | DataProperty], self._nodes("equivalents", value))

    def component(self, value: ObjectProperty | DataProperty) -> PropertyComponent:
        if not isinstance(value, (ObjectProperty, DataProperty)):
            raise TypeError("value must be a named object/data property")
        node = next(self._nodes("component", value), value)
        return node if isinstance(node, PropertyComponent) else PropertyComponent((node,))

    def direct_parents(self, value: PropertyHierarchyNode) -> Iterator[PropertyHierarchyNode]:
        if self.options.equivalence_handling.value != "component":
            raise ValueError("direct hierarchy queries require component mode")
        yield from self._nodes("direct_parents", value)

    def direct_children(self, value: PropertyHierarchyNode) -> Iterator[PropertyHierarchyNode]:
        if self.options.equivalence_handling.value != "component":
            raise ValueError("direct hierarchy queries require component mode")
        yield from self._nodes("direct_children", value)

    @property
    def non_named_endpoint_count(self) -> int:
        return cast(int, self.native_report["ignored_complex_endpoint_count"])
