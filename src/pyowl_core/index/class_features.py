"""Native named-class structural features with explicit operand projection.

This is syntax-only graph reduction, not a complete OWL reasoner. Named
EquivalentClasses operands form components. Named SubClassOf endpoints supply
edges. With equivalent_operands=True, an equivalent intersection supplies edges
from each named anchor to its named operands; an equivalent union supplies the
reverse edges. No other complex constructors contribute graph edges.

Direct parents remove a candidate reachable from another candidate; children are
its inverse. Cycles keep this definition (they are not silently collapsed).
Class results use exact IRI order. Restriction expressions preserve first occurrence
in canonical SubClassOf order followed by canonical EquivalentClasses order.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from pyowl_core.cancellation import CancellationToken
from pyowl_core.document import AxiomScope, OntologyOverlay, OntologyView
from pyowl_core.exceptions import BackendProtocolError
from pyowl_core.model import IRI, Class, ClassExpression, decode_canonical
from pyowl_core.model.axioms import EquivalentClasses, SubClassOf

from .cache import IndexBuildBudget, ViewBuildReport, ViewBuildStrategy, build_report
from .common import ScopedIndexOptions


@dataclass(frozen=True, slots=True)
class ClassFeatureOptions(ScopedIndexOptions):
    include_origins: bool = False
    equivalent_operands: bool = False
    include_builtins: bool = True
    require_native_pipeline: bool = False

    def __post_init__(self) -> None:
        ScopedIndexOptions.__post_init__(self)
        for name in ("equivalent_operands", "include_builtins", "require_native_pipeline"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if self.include_origins:
            raise ValueError("ClassFeatureView returns expressions/classes without origins")


class ClassFeatureView:
    """Retained native graph and selected class-expression rows; no scalar fallback."""

    SCHEMA_NAME = "pyowl-core/class-features"
    SCHEMA_VERSION = 1
    OPTIONS_TYPE = ClassFeatureOptions
    DEPENDENCIES: tuple[type[object], ...] = ()
    _ontology: OntologyView
    options: ClassFeatureOptions
    report: ViewBuildReport
    _native_index: Any
    _extension: Any

    @staticmethod
    def supports_native() -> bool:
        """Probe the installed binary before loading an ontology."""
        import importlib

        try:
            extension = importlib.import_module("pyowl_core._native")
        except (ImportError, OSError):
            return False
        version = getattr(extension, "NATIVE_CLASS_FEATURES_API_VERSION", None)
        return (
            type(version) is int
            and version == 1
            and callable(getattr(extension, "_class_features_v1", None))
        )

    @classmethod
    def _build(
        cls,
        ontology: object,
        options: object,
        budget: IndexBuildBudget,
        cancellation_token: CancellationToken | None,
        started: float,
    ) -> ClassFeatureView:
        from pyowl_core.backends.native_validation import _native_owner
        from pyowl_core.backends.native_views import (
            _invoke_native_column_operation_v2,
            _selected_limits,
        )

        if not isinstance(options, ClassFeatureOptions):
            raise TypeError("options must be ClassFeatureOptions")
        view = cls()
        view._ontology = cast(OntologyView, ontology)
        view.options = options
        if isinstance(ontology, OntologyOverlay):
            if options.scope is AxiomScope.CLOSURE and any(
                isinstance(axiom, (SubClassOf, EquivalentClasses))
                for axiom in (*ontology.delta.add_axioms, *ontology.delta.remove_axioms)
            ):
                raise BackendProtocolError(
                    "native class feature overlay changes unsupported", code="NATIVE_VIEW_REQUIRED"
                )
            inherited = ontology.base.view(
                cls,
                scope=options.scope,
                document_key=options.document_key,
                equivalent_operands=options.equivalent_operands,
                include_builtins=options.include_builtins,
                require_native_pipeline=True,
                cancellation_token=cancellation_token,
            )
            view._native_index = inherited._native_index
            view._extension = inherited._extension
            budget.add_shared_rows(inherited.report.total_row_count)
            view.report = build_report(
                cls,
                ViewBuildStrategy.PATCHED,
                budget,
                started,
                shared_bytes=inherited.report.own_bytes + inherited.report.shared_bytes,
            )
            return view
        selected = _native_owner(view._ontology)
        if selected is None or getattr(selected[1], "NATIVE_CLASS_FEATURES_API_VERSION", None) != 1:
            raise BackendProtocolError(
                "native class features unavailable for this owner", code="NATIVE_VIEW_REQUIRED"
            )
        raw, view._extension = selected
        scope, ordinal = cast(Any, ontology)._native_scope(options.scope, options.document_key)
        view._native_index = _invoke_native_column_operation_v2(
            view._extension,
            view._extension._class_features_v1,
            (raw, scope.value, ordinal, options.equivalent_operands, options.include_builtins),
            _selected_limits(view._ontology, None),
            cancellation_token,
        )
        observed = cast(Any, view._native_index)._report_v1()
        budget.add(
            "native_class_features",
            rows=observed["selected_axioms"],
            bytes_=observed["allocation_charge_bytes"],
        )
        view.report = build_report(cls, ViewBuildStrategy.FULL_BUILD, budget, started)
        return view

    @property
    def native_report(self) -> Mapping[str, object]:
        cast(Any, self._ontology)._check_open()
        return MappingProxyType(cast(Any, self._native_index)._report_v1())

    def _query(self, kind: str, value: Class, cancellation_token: CancellationToken | None) -> Any:
        from pyowl_core.backends.native_views import (
            _invoke_native_column_operation_v2,
            _selected_limits,
        )

        if not isinstance(value, Class):
            raise TypeError("value must be Class")
        cast(Any, self._ontology)._check_open()
        return _invoke_native_column_operation_v2(
            self._extension,
            cast(Any, self._native_index)._query_v1,
            (kind, value.iri.value),
            _selected_limits(self._ontology, None),
            cancellation_token,
        )

    def restrictions(
        self, value: Class, *, cancellation_token: CancellationToken | None = None
    ) -> Iterator[ClassExpression]:
        """Return this named subject's super/equivalent nonnamed expressions."""
        for row in self._query("restrictions", value, cancellation_token):
            yield cast(ClassExpression, decode_canonical(row))

    def _classes(
        self, kind: str, value: Class, cancellation_token: CancellationToken | None
    ) -> tuple[Class, ...]:
        return tuple(
            Class(IRI(row.decode("utf-8"))) for row in self._query(kind, value, cancellation_token)
        )

    def direct_parents(
        self, value: Class, *, cancellation_token: CancellationToken | None = None
    ) -> tuple[Class, ...]:
        return self._classes("parents", value, cancellation_token)

    def direct_children(
        self, value: Class, *, cancellation_token: CancellationToken | None = None
    ) -> tuple[Class, ...]:
        return self._classes("children", value, cancellation_token)

    def ancestors(
        self, value: Class, *, cancellation_token: CancellationToken | None = None
    ) -> tuple[Class, ...]:
        return self._classes("ancestors", value, cancellation_token)

    def descendants(
        self, value: Class, *, cancellation_token: CancellationToken | None = None
    ) -> tuple[Class, ...]:
        return self._classes("descendants", value, cancellation_token)

    def component(
        self, value: Class, *, cancellation_token: CancellationToken | None = None
    ) -> tuple[Class, ...]:
        return self._classes("component", value, cancellation_token) or (value,)
