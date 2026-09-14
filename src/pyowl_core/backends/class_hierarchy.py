"""Requested result wrappers for the retained native asserted class index."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import AxiomScope, OntologyOverlay, OntologyView
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.index.cache import IndexBuildBudget, ViewBuildStrategy, build_report
from pyowl_core.index.hierarchy import (
    AssertedClassHierarchyView,
    ClassComponent,
    ClassEquivalenceRecord,
    ClassHierarchyEdge,
    ClassHierarchyNode,
    ClassHierarchyOptions,
)
from pyowl_core.model import Class, canonical_bytes, decode_canonical
from pyowl_core.model.axioms import AxiomNode, DisjointUnion, EquivalentClasses, SubClassOf

from .native_validation import _native_owner
from .native_views import _invoke_native_column_operation_v2, _selected_limits


def build_native_class_hierarchy(
    ontology: OntologyView,
    options: ClassHierarchyOptions,
    budget: IndexBuildBudget,
    cancellation_token: CancellationToken | None,
    started: float,
) -> AssertedClassHierarchyView:
    if isinstance(ontology, OntologyOverlay):
        relevant = (SubClassOf, EquivalentClasses, DisjointUnion)
        if options.scope is AxiomScope.CLOSURE and any(
            isinstance(axiom, relevant)
            for axiom in (*ontology.delta.add_axioms, *ontology.delta.remove_axioms)
        ):
            raise BackendProtocolError(
                "native class hierarchy changes in overlays are unsupported",
                code="NATIVE_VIEW_REQUIRED",
            )
        # The explicit delta contains no selected constructor; ROOT/DOCUMENT
        # scopes always delegate unchanged. Retain the original overlay owner.
        inherited = ontology.base.view(
            AssertedClassHierarchyView,
            scope=options.scope,
            document_key=options.document_key,
            include_origins=options.include_origins,
            equivalence_handling=options.equivalence_handling,
            include_disjoint_union=options.include_disjoint_union,
            require_native_pipeline=True,
            cancellation_token=cancellation_token,
        )
        if not isinstance(inherited, _NativeClassHierarchyView):
            raise BackendProtocolError(
                "native class hierarchy receipt unavailable", code="NATIVE_VIEW_REQUIRED"
            )
        budget.add_shared_rows(inherited.report.total_row_count)
        view = _NativeClassHierarchyView.__new__(_NativeClassHierarchyView)
        view._ontology = ontology
        view.options = options
        view._native_index = inherited._native_index
        view._native_extension = inherited._native_extension
        view.report = build_report(
            AssertedClassHierarchyView,
            ViewBuildStrategy.PATCHED,
            budget,
            started,
            shared_bytes=inherited.report.own_bytes + inherited.report.shared_bytes,
        )
        return view
    selected = _native_owner(ontology)
    if selected is None or getattr(selected[1], "NATIVE_CLASS_HIERARCHY_API_VERSION", None) != 1:
        raise BackendProtocolError(
            "native asserted class hierarchy unavailable for this owner",
            code="NATIVE_VIEW_REQUIRED",
        )
    raw, extension = selected
    scope, ordinal = cast(Any, ontology)._native_scope(options.scope, options.document_key)
    index = _invoke_native_column_operation_v2(
        extension,
        extension._class_hierarchy_v1,
        (
            raw,
            scope.value,
            ordinal,
            options.equivalence_handling.value,
            options.include_disjoint_union,
        ),
        _selected_limits(ontology, None),
        cancellation_token,
    )
    observed = cast(Any, index)._report_v1()
    budget.add(
        "native_class_hierarchy",
        rows=observed["selected_axioms"],
        bytes_=observed["allocation_charge_bytes"],
    )
    view = _NativeClassHierarchyView.__new__(_NativeClassHierarchyView)
    view._ontology = ontology
    view.options = options
    view._native_index = index
    view._native_extension = extension
    view.report = build_report(
        AssertedClassHierarchyView, ViewBuildStrategy.FULL_BUILD, budget, started
    )
    return view


class _NativeClassHierarchyView(AssertedClassHierarchyView):
    _native_index: Any
    _native_extension: Any

    @property
    def native_report(self) -> Mapping[str, object]:
        cast(Any, self._ontology)._check_open()
        return MappingProxyType(self._native_index._report_v1())

    def _node(self, value: Any) -> ClassHierarchyNode:
        members, component = value
        classes = tuple(cast(Class, decode_canonical(member)) for member in members)
        return ClassComponent(classes) if component else classes[0]

    def _nodes(self, kind: str, value: ClassHierarchyNode) -> Iterator[ClassHierarchyNode]:
        cast(Any, self._ontology)._check_open()
        if not isinstance(value, (Class, ClassComponent)):
            raise TypeError("value must be Class or ClassComponent")
        members = value.members if isinstance(value, ClassComponent) else (value,)
        rows = _invoke_native_column_operation_v2(
            self._native_extension,
            self._native_index._nodes_v1,
            (
                kind,
                [canonical_bytes(member) for member in members],
                isinstance(value, ClassComponent),
            ),
            _selected_limits(self._ontology, None),
            None,
        )
        yield from (self._node(row) for row in cast(Any, rows))

    def _records(self, kind: str, limit: int | None) -> Iterator[Any]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a nonnegative integer or None")
        cursor = 0
        while limit is None or cursor < limit:
            cast(Any, self._ontology)._check_open()
            count = 64 if limit is None else min(64, limit - cursor)
            rows = _invoke_native_column_operation_v2(
                self._native_extension,
                self._native_index._records_v1,
                (kind, cursor, count),
                _selected_limits(self._ontology, None),
                None,
            )
            page = cast(Any, rows)
            yield from page
            cursor += len(page)
            if len(page) < count:
                return

    def _origins(self, digest: bytes) -> Any:
        owner = self._ontology
        while isinstance(owner, OntologyOverlay):
            owner = owner.base
        return owner.origin_index.entries.get(digest, ()) if self.options.include_origins else ()

    def iter_edges(self, *, limit: int | None = None) -> Iterator[ClassHierarchyEdge]:
        for child, parent, axiom, digest in self._records("edges", limit):
            yield ClassHierarchyEdge(
                self._node(child),
                self._node(parent),
                cast(AxiomNode, decode_canonical(axiom)),
                self._origins(digest),
            )

    def equivalence_sets(self, *, limit: int | None = None) -> Iterator[ClassEquivalenceRecord]:
        for members, axiom, digest in self._records("equivalences", limit):
            yield ClassEquivalenceRecord(
                tuple(cast(Class, decode_canonical(member)) for member in members),
                cast(EquivalentClasses, decode_canonical(axiom)),
                self._origins(digest),
            )

    def asserted_parents(self, value: ClassHierarchyNode) -> Iterator[ClassHierarchyNode]:
        yield from self._nodes("parents", value)

    def asserted_children(self, value: ClassHierarchyNode) -> Iterator[ClassHierarchyNode]:
        yield from self._nodes("children", value)

    def equivalents(self, value: Class) -> Iterator[Class]:
        if not isinstance(value, Class):
            raise TypeError("value must be Class")
        yield from cast(Iterator[Class], self._nodes("equivalents", value))

    def component(self, value: Class) -> ClassComponent:
        if not isinstance(value, Class):
            raise TypeError("value must be Class")
        node = next(self._nodes("component", value), value)
        return node if isinstance(node, ClassComponent) else ClassComponent((node,))

    @property
    def ignored_complex_endpoint_count(self) -> int:
        return cast(int, self.native_report["ignored_complex_endpoint_count"])
