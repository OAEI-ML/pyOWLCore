"""Persistent read-through ontology overlays with explicit compaction."""

from __future__ import annotations

import hashlib
import heapq
import threading
import time
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import chain
from typing import Generic, TypeVar, cast

from pyowl_core._immutable import freeze_mapping
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.exceptions import (
    DeltaBaseMismatchError,
    DeltaError,
    OverlayPerformanceWarning,
    ResourceLimitError,
)
from pyowl_core.limits import ParseLimits
from pyowl_core.model import (
    Annotation,
    AnonymousIndividual,
    CanonicalSet,
    Entity,
    EntityKind,
    StructuralNode,
    canonical_bytes,
    structural_digest,
    walk,
)
from pyowl_core.model import signature as node_signature
from pyowl_core.model.axioms import AxiomNode

from .delta import DeltaPolicy, OntologyDelta, combine_deltas
from .document import Fingerprint
from .fingerprint import (
    StructuralContext,
    StructuralContextKind,
    effective_structural_fingerprint,
    fingerprint_bytes,
    logical_fingerprint,
    signature_fingerprint,
)
from .provenance import OriginIndex, OriginOccurrence
from .snapshot import (
    AxiomScope,
    CoreCapabilities,
    LoadReport,
    OntologySnapshot,
    OntologyView,
    _is_ontology_view,
    materialize_view,
)

A = TypeVar("A", bound=AxiomNode)
V = TypeVar("V")
T = TypeVar("T")


class _SizedIterable(Generic[T]):
    __slots__ = ("_factory", "_size")

    def __init__(self, size: int, factory: Callable[[], Iterator[T]]) -> None:
        self._size = size
        self._factory = factory

    def __len__(self) -> int:
        return self._size

    def __iter__(self) -> Iterator[T]:
        return self._factory()


@dataclass(frozen=True, slots=True, eq=False, init=False)
class OntologyOverlay:
    """One immutable closure-delta layer; root/document scopes delegate to the base."""

    base: OntologyView
    delta: OntologyDelta
    requested_delta: OntologyDelta = field(repr=False, compare=False)
    no_op_add_axioms: CanonicalSet[AxiomNode] = field(repr=False, compare=False)
    no_op_remove_axioms: CanonicalSet[AxiomNode] = field(repr=False, compare=False)
    no_op_add_ontology_annotations: CanonicalSet[Annotation] = field(repr=False, compare=False)
    no_op_remove_ontology_annotations: CanonicalSet[Annotation] = field(repr=False, compare=False)
    depth: int
    limits: ParseLimits = field(repr=False, compare=False)
    _anchor: OntologyView = field(repr=False, compare=False)
    _cumulative_delta: OntologyDelta = field(repr=False, compare=False)
    _edit_chain: tuple[bytes, ...] = field(repr=False, compare=False)
    _edit_chain_digest_cache: bytes | None = field(repr=False, compare=False)
    _addition_origins: Mapping[StructuralNode, tuple[int, int]] = field(repr=False, compare=False)
    _structural_context_cache: StructuralContext | None = field(repr=False, compare=False)
    _capabilities: CoreCapabilities = field(repr=False, compare=False)
    _dependency_token: object | None = field(repr=False, compare=False)
    _fingerprint_cache: tuple[Fingerprint, Fingerprint, Fingerprint] | None = field(
        repr=False, compare=False
    )
    _report_cache: LoadReport | None = field(repr=False, compare=False)
    _origin_cache: OriginIndex | None = field(repr=False, compare=False)
    _cache_lock: threading.Lock = field(repr=False, compare=False)

    def __init__(self, base: OntologyView, delta: OntologyDelta) -> None:
        self._initialize(base, delta)

    def _initialize(
        self,
        base: OntologyView,
        requested: OntologyDelta,
        *,
        validated: bool = False,
        anchor: OntologyView | None = None,
        cumulative: OntologyDelta | None = None,
        depth: int | None = None,
        context: StructuralContext | None = None,
        edit_chain: tuple[bytes, ...] | None = None,
        edit_chain_digest_cache: bytes | None = None,
        addition_origins: Mapping[StructuralNode, tuple[int, int]] | None = None,
        no_op_state: tuple[
            CanonicalSet[AxiomNode],
            CanonicalSet[AxiomNode],
            CanonicalSet[Annotation],
            CanonicalSet[Annotation],
        ]
        | None = None,
    ) -> None:
        _require_view(base)
        if not isinstance(requested, OntologyDelta):
            raise TypeError("delta must be OntologyDelta")
        _ensure_live(base)
        limits = view_limits(base)
        limits.enforce("max_delta_entries", requested.entry_count)
        if validated:
            effective = requested
            no_op_add = CanonicalSet[AxiomNode]()
            no_op_remove = CanonicalSet[AxiomNode]()
            no_op_annotation_add = CanonicalSet[Annotation]()
            no_op_annotation_remove = CanonicalSet[Annotation]()
        else:
            (
                effective,
                no_op_add,
                no_op_remove,
                no_op_annotation_add,
                no_op_annotation_remove,
            ) = validate_delta(base, requested)
        if no_op_state is not None:
            (
                no_op_add,
                no_op_remove,
                no_op_annotation_add,
                no_op_annotation_remove,
            ) = no_op_state
        selected_depth = base.depth + 1 if isinstance(base, OntologyOverlay) else 1
        if depth is not None:
            selected_depth = depth
        if selected_depth > limits.max_overlay_depth:
            raise ResourceLimitError(
                "resource limit max_overlay_depth exceeded",
                limit="max_overlay_depth",
                observed=selected_depth,
                allowed=limits.max_overlay_depth,
            )
        selected_anchor = (
            (base._anchor if isinstance(base, OntologyOverlay) else base)
            if anchor is None
            else anchor
        )
        selected_cumulative = (
            combine_deltas(base._cumulative_delta, effective)
            if isinstance(base, OntologyOverlay)
            else effective
        )
        if cumulative is not None:
            selected_cumulative = cumulative
        limits.enforce("max_delta_entries", selected_cumulative.entry_count)
        selected_edit_chain = (
            (
                *(base._edit_chain if isinstance(base, OntologyOverlay) else ()),
                requested.provenance_digest,
            )
            if edit_chain is None
            else edit_chain
        )
        if addition_origins is None:
            active_origins = (
                dict(base._addition_origins) if isinstance(base, OntologyOverlay) else {}
            )
            for value in (*effective.remove_axioms, *effective.remove_ontology_annotations):
                active_origins.pop(value, None)
            for occurrence, value in enumerate(
                (*effective.add_axioms, *effective.add_ontology_annotations)
            ):
                active_origins[value] = (len(selected_edit_chain), occurrence)
            active_additions: CanonicalSet[StructuralNode] = CanonicalSet(
                (
                    *selected_cumulative.add_axioms,
                    *selected_cumulative.add_ontology_annotations,
                )
            )
            active_origins = {
                value: origin
                for value, origin in active_origins.items()
                if value in active_additions
            }
        else:
            active_origins = dict(addition_origins)
        if not validated and selected_depth == 32:
            warnings.warn(
                "overlay depth reached the default compaction recommendation threshold",
                OverlayPerformanceWarning,
                stacklevel=3,
            )
        base_size = _known_effective_axiom_count(selected_anchor)
        previous_delta_size = (
            base._cumulative_delta.entry_count if isinstance(base, OntologyOverlay) else 0
        )
        if (
            not validated
            and base_size is not None
            and base_size >= 10
            and selected_cumulative.entry_count > max(1, base_size // 10)
            and previous_delta_size <= max(1, base_size // 10)
        ):
            warnings.warn(
                "overlay delta exceeds ten percent of the effective base",
                OverlayPerformanceWarning,
                stacklevel=3,
            )
        features = set(base.capabilities.features)
        if not effective.is_empty:
            features.discard("owl2-dl-validated")
        features.update({"ontology-overlay", "persistent-delta", "zero-copy-view"})
        capabilities = CoreCapabilities(
            base.capabilities.adapter_protocol,
            base.capabilities.model_schema,
            base.capabilities.wire_format,
            frozenset(features),
            base.capabilities.encoded_view_schemas,
            base.capabilities.backend,
        )
        object.__setattr__(self, "base", base)
        object.__setattr__(self, "delta", effective)
        object.__setattr__(self, "requested_delta", requested)
        object.__setattr__(self, "no_op_add_axioms", no_op_add)
        object.__setattr__(self, "no_op_remove_axioms", no_op_remove)
        object.__setattr__(self, "no_op_add_ontology_annotations", no_op_annotation_add)
        object.__setattr__(self, "no_op_remove_ontology_annotations", no_op_annotation_remove)
        object.__setattr__(self, "depth", selected_depth)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "_anchor", selected_anchor)
        object.__setattr__(self, "_cumulative_delta", selected_cumulative)
        object.__setattr__(self, "_edit_chain", selected_edit_chain)
        object.__setattr__(self, "_edit_chain_digest_cache", edit_chain_digest_cache)
        object.__setattr__(self, "_addition_origins", freeze_mapping(active_origins))
        object.__setattr__(self, "_structural_context_cache", context)
        object.__setattr__(self, "_capabilities", capabilities)
        object.__setattr__(self, "_dependency_token", _retain_dependency(base))
        object.__setattr__(self, "_fingerprint_cache", None)
        object.__setattr__(self, "_report_cache", None)
        object.__setattr__(self, "_origin_cache", None)
        object.__setattr__(self, "_cache_lock", threading.Lock())

    @classmethod
    def _compacted(cls, value: OntologyOverlay) -> OntologyOverlay:
        compacted = object.__new__(cls)
        compacted._initialize(
            value._anchor,
            value._cumulative_delta,
            validated=True,
            anchor=value._anchor,
            cumulative=value._cumulative_delta,
            depth=1,
            context=value._structural_context_cache,
            edit_chain=value._edit_chain,
            edit_chain_digest_cache=value._edit_chain_digest_cache,
            addition_origins=value._addition_origins,
            no_op_state=value._all_no_ops(),
        )
        return compacted

    @property
    def capabilities(self) -> CoreCapabilities:
        _ensure_live(self.base)
        return self._capabilities

    def _check_open(self) -> None:
        _ensure_live(self.base)

    def _anonymous_document_scopes(self) -> frozenset[bytes] | None:
        inherited = getattr(self.base, "_anonymous_document_scopes", None)
        if not callable(inherited) or self._delta_changes_anonymous():
            return None
        result = inherited()
        return result if isinstance(result, frozenset) else None

    def _anonymous_scope_lineage(self) -> tuple[tuple[bytes, bytes, bytes], ...] | None:
        scopes = self._anonymous_document_scopes()
        if scopes is None:
            return None
        leaf = fingerprint_bytes(self.structural_fingerprint)
        return tuple((scope, scope, leaf) for scope in sorted(scopes))

    def _delta_changes_anonymous(self) -> bool:
        changed_values = chain(
            self.delta.add_axioms,
            self.delta.remove_axioms,
            self.delta.add_ontology_annotations,
            self.delta.remove_ontology_annotations,
        )
        return any(
            isinstance(node, AnonymousIndividual)
            for value in changed_values
            for node in walk(value)
        )

    @property
    def is_complete(self) -> bool:
        _ensure_live(self.base)
        return self.base.is_complete

    @property
    def structural_context(self) -> StructuralContext:
        retained = self._structural_context_cache
        if retained is not None:
            return retained
        created = StructuralContext.overlay(_overlay_anchor_fingerprint(self._anchor))
        with self._cache_lock:
            retained = self._structural_context_cache
            if retained is None:
                object.__setattr__(self, "_structural_context_cache", created)
                return created
            return retained

    @property
    def edit_chain_digest(self) -> bytes:
        retained = self._edit_chain_digest_cache
        if retained is not None:
            return retained
        created = self._edit_chain_digest_at(len(self._edit_chain))
        with self._cache_lock:
            retained = self._edit_chain_digest_cache
            if retained is None:
                object.__setattr__(self, "_edit_chain_digest_cache", created)
                return created
            return retained

    def _edit_chain_digest_at(self, length: int) -> bytes:
        created = self._anchor.structural_fingerprint.digest
        for delta_digest in self._edit_chain[:length]:
            created = hashlib.sha256(
                b"pyowl-core:overlay-edit-chain:v1\x00" + created + delta_digest
            ).digest()
        return created

    @property
    def structural_fingerprint(self) -> Fingerprint:
        return self._fingerprints()[0]

    @property
    def logical_fingerprint(self) -> Fingerprint:
        return self._fingerprints()[1]

    @property
    def signature_fingerprint(self) -> Fingerprint:
        return self._fingerprints()[2]

    @property
    def report(self) -> LoadReport:
        _ensure_live(self.base)
        retained = self._report_cache
        if retained is not None:
            return retained
        started = time.monotonic()
        structural, logical, signature_value = self._fingerprints()
        base_report = self.base.report
        diagnostics = (*base_report.diagnostics, *self._no_op_diagnostics())
        timings = dict(base_report.timings)
        timings["overlay_fingerprint_seconds"] = time.monotonic() - started
        created = LoadReport(
            base_report.backend,
            base_report.api_version,
            base_report.model_schema,
            base_report.document_count,
            base_report.total_source_bytes,
            self.effective_axiom_count,
            base_report.resolution_attempts,
            base_report.acquisition_cache_hits,
            base_report.document_cache_hits,
            timings,
            diagnostics,
            structural,
            logical,
            signature_value,
            None,
        )
        with self._cache_lock:
            retained = self._report_cache
            if retained is None:
                object.__setattr__(self, "_report_cache", created)
                return created
            return retained

    @property
    def effective_axiom_count(self) -> int:
        base_size = _known_effective_axiom_count(self.base)
        if base_size is None:
            base_size = sum(1 for _ in self.base.iter_axioms())
        return base_size + len(self.delta.add_axioms) - len(self.delta.remove_axioms)

    @property
    def origin_index(self) -> OriginIndex:
        _ensure_live(self.base)
        retained = self._origin_cache
        if retained is not None:
            return retained
        entries: dict[bytes, tuple[OriginOccurrence, ...]] = {}
        observed = 0
        roots = chain(
            self.ontology_annotations(),
            self.iter_axioms(),
            self.iter_extensions(),
        )
        for value in roots:
            origins = self.origins_for(value)
            if origins:
                entries[structural_digest(value)] = origins
                observed += len(origins)
                self.limits.enforce("max_origin_entries", observed)
        created = OriginIndex(entries)
        with self._cache_lock:
            retained = self._origin_cache
            if retained is None:
                object.__setattr__(self, "_origin_cache", created)
                return created
            return retained

    def origins_for(self, value: StructuralNode) -> tuple[OriginOccurrence, ...]:
        if not isinstance(value, StructuralNode):
            raise TypeError("value must be StructuralNode")
        addition = self._addition_origins.get(value)
        if addition is not None:
            chain_length, occurrence = addition
            key = "delta:" + self._edit_chain_digest_at(chain_length).hex()
            return (OriginOccurrence(key, occurrence),)
        if value in self._cumulative_delta.remove_axioms or value in (
            self._cumulative_delta.remove_ontology_annotations
        ):
            return ()
        method = getattr(self._anchor, "origins_for", None)
        if callable(method):
            result = method(value)
            return tuple(result)
        return self._anchor.origin_index.origins_for(value)

    def iter_axioms(
        self,
        axiom_type: type[A] | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[AxiomNode | A]:
        _ensure_live(self.base)
        _validate_axiom_type(axiom_type)
        if scope is not AxiomScope.CLOSURE:
            yield from self.base.iter_axioms(axiom_type, scope=scope, document_key=document_key)
            return
        additions: Iterable[AxiomNode] = self.delta.add_axioms
        if axiom_type is not None:
            additions = (item for item in additions if type(item) is axiom_type)
        base_values = self.base.iter_axioms(axiom_type, scope=scope, document_key=document_key)
        yield from cast(
            Iterator[AxiomNode | A],
            canonical_merge(
                (base_values, iter(additions)),
                excluded=self.delta.remove_axioms,
            ),
        )

    def iter_extensions(
        self,
        namespace: str | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[StructuralNode]:
        _ensure_live(self.base)
        yield from self.base.iter_extensions(namespace, scope=scope, document_key=document_key)

    def ontology_annotations(
        self,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> CanonicalSet[Annotation]:
        _ensure_live(self.base)
        if scope is not AxiomScope.CLOSURE:
            return view_annotations(self.base, scope=scope, document_key=document_key)
        base_values = view_annotations(self.base, scope=scope, document_key=document_key)
        return CanonicalSet(
            (
                *(
                    item
                    for item in base_values
                    if item not in self.delta.remove_ontology_annotations
                ),
                *self.delta.add_ontology_annotations,
            )
        )

    def contains(
        self,
        axiom: AxiomNode,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> bool:
        _ensure_live(self.base)
        if not isinstance(axiom, AxiomNode):
            raise TypeError("axiom must be an OWL axiom")
        if scope is not AxiomScope.CLOSURE:
            return self.base.contains(axiom, scope=scope, document_key=document_key)
        return axiom in self.delta.add_axioms or (
            axiom not in self.delta.remove_axioms
            and self.base.contains(axiom, scope=scope, document_key=document_key)
        )

    def signature(
        self,
        kind: EntityKind | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
        include_builtins: bool = True,
    ) -> tuple[Entity, ...]:
        if kind is not None and not isinstance(kind, EntityKind):
            raise TypeError("kind must be EntityKind or None")
        if not isinstance(include_builtins, bool):
            raise TypeError("include_builtins must be bool")
        roots = chain(
            self.ontology_annotations(scope=scope, document_key=document_key),
            self.iter_axioms(scope=scope, document_key=document_key),
            self.iter_extensions(scope=scope, document_key=document_key),
        )
        return collect_signature(roots, kind, include_builtins=include_builtins)

    def view(self, view_type: type[V], /, **options: object) -> V:
        if not isinstance(view_type, type):
            raise TypeError("view_type must be a type")
        if options:
            raise TypeError("OntologyOverlay identity view accepts no options")
        if view_type is OntologyOverlay or isinstance(self, view_type):
            return cast(V, self)
        raise LookupError(f"view type {view_type.__name__} is not available")

    def compact(self) -> OntologyOverlay:
        """Collapse the edit chain without iterating or copying the anchor."""

        _ensure_live(self.base)
        return self if self.depth == 1 else OntologyOverlay._compacted(self)

    def materialize(self) -> OntologySnapshot:
        """Explicitly copy effective collections into an independent snapshot."""

        _ensure_live(self.base)
        started = time.monotonic()
        annotations = self.ontology_annotations()
        axioms = CanonicalSet(self.iter_axioms())
        extensions = CanonicalSet(self.iter_extensions())
        return materialize_view(
            self,
            annotations=annotations,
            axioms=axioms,
            extensions=extensions,
            origin_index=self.origin_index,
            structural_context=self.structural_context,
            structural_fingerprint_override=(
                self._anchor.structural_fingerprint if self._cumulative_delta.is_empty else None
            ),
            limits=self.limits,
            elapsed_seconds=time.monotonic() - started,
        )

    def _fingerprints(self) -> tuple[Fingerprint, Fingerprint, Fingerprint]:
        _ensure_live(self.base)
        retained = self._fingerprint_cache
        if retained is not None:
            return retained
        if self._cumulative_delta.is_empty:
            created = (
                self._anchor.structural_fingerprint,
                self._anchor.logical_fingerprint,
                self._anchor.signature_fingerprint,
            )
            with self._cache_lock:
                retained = self._fingerprint_cache
                if retained is None:
                    object.__setattr__(self, "_fingerprint_cache", created)
                    return created
                return retained
        annotations = self.ontology_annotations()
        extension_count = sum(1 for _ in self.iter_extensions())
        structural = effective_structural_fingerprint(
            self.structural_context,
            annotations,
            _SizedIterable(self.effective_axiom_count, lambda: self.iter_axioms()),
            _SizedIterable(extension_count, lambda: self.iter_extensions()),
        )
        logical = logical_fingerprint(self.iter_axioms(), self.iter_extensions())
        signature_value = signature_fingerprint(self.signature())
        created = (structural, logical, signature_value)
        with self._cache_lock:
            retained = self._fingerprint_cache
            if retained is None:
                object.__setattr__(self, "_fingerprint_cache", created)
                return created
            return retained

    def _no_op_diagnostics(self) -> tuple[Diagnostic, ...]:
        values: list[Diagnostic] = []
        count = len(self.no_op_add_axioms) + len(self.no_op_add_ontology_annotations)
        if count:
            values.append(
                Diagnostic(
                    "DELTA_IDEMPOTENT_ADD_NOOP",
                    Severity.INFO,
                    "idempotent delta additions were already present",
                    details={"count": count},
                )
            )
        count = len(self.no_op_remove_axioms) + len(self.no_op_remove_ontology_annotations)
        if count:
            values.append(
                Diagnostic(
                    "DELTA_IDEMPOTENT_REMOVE_NOOP",
                    Severity.INFO,
                    "idempotent delta removals were already absent",
                    details={"count": count},
                )
            )
        return tuple(values)

    def _all_no_ops(
        self,
    ) -> tuple[
        CanonicalSet[AxiomNode],
        CanonicalSet[AxiomNode],
        CanonicalSet[Annotation],
        CanonicalSet[Annotation],
    ]:
        axiom_add: list[AxiomNode] = []
        axiom_remove: list[AxiomNode] = []
        annotation_add: list[Annotation] = []
        annotation_remove: list[Annotation] = []
        current: OntologyView = self
        while isinstance(current, OntologyOverlay):
            axiom_add.extend(current.no_op_add_axioms)
            axiom_remove.extend(current.no_op_remove_axioms)
            annotation_add.extend(current.no_op_add_ontology_annotations)
            annotation_remove.extend(current.no_op_remove_ontology_annotations)
            current = current.base
        return (
            CanonicalSet(axiom_add),
            CanonicalSet(axiom_remove),
            CanonicalSet(annotation_add),
            CanonicalSet(annotation_remove),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OntologyOverlay):
            return NotImplemented
        return self.structural_fingerprint == other.structural_fingerprint

    def __hash__(self) -> int:
        value = int.from_bytes(self.structural_fingerprint.digest[:8], "big", signed=True)
        return -2 if value == -1 else value


def apply_delta(base: OntologyView, delta: OntologyDelta) -> OntologyOverlay:
    """Create one validated persistent overlay without walking the base."""

    return OntologyOverlay(base, delta)


def validate_delta(
    base: OntologyView,
    requested: OntologyDelta,
) -> tuple[
    OntologyDelta,
    CanonicalSet[AxiomNode],
    CanonicalSet[AxiomNode],
    CanonicalSet[Annotation],
    CanonicalSet[Annotation],
]:
    """Bind and reduce a requested delta against one effective base."""

    expected = requested.expected_base_fingerprint
    if expected is not None and expected != base.structural_fingerprint:
        raise DeltaBaseMismatchError(
            "delta expected-base fingerprint does not match the supplied view",
            code="DELTA_BASE_MISMATCH",
        )
    add: list[AxiomNode] = []
    remove: list[AxiomNode] = []
    no_op_add: list[AxiomNode] = []
    no_op_remove: list[AxiomNode] = []
    for item in requested.add_axioms:
        if base.contains(item):
            if requested.policy is DeltaPolicy.STRICT:
                raise DeltaError("delta adds an existing axiom", code="DELTA_ADD_EXISTS")
            no_op_add.append(item)
        else:
            add.append(item)
    for item in requested.remove_axioms:
        if not base.contains(item):
            if requested.policy is DeltaPolicy.STRICT:
                raise DeltaError("delta removes an absent axiom", code="DELTA_REMOVE_ABSENT")
            no_op_remove.append(item)
        else:
            remove.append(item)
    annotations = view_annotations(base)
    annotation_add: list[Annotation] = []
    annotation_remove: list[Annotation] = []
    no_op_annotation_add: list[Annotation] = []
    no_op_annotation_remove: list[Annotation] = []
    for annotation in requested.add_ontology_annotations:
        if annotation in annotations:
            if requested.policy is DeltaPolicy.STRICT:
                raise DeltaError(
                    "delta adds an existing ontology annotation",
                    code="DELTA_ANNOTATION_ADD_EXISTS",
                )
            no_op_annotation_add.append(annotation)
        else:
            annotation_add.append(annotation)
    for annotation in requested.remove_ontology_annotations:
        if annotation not in annotations:
            if requested.policy is DeltaPolicy.STRICT:
                raise DeltaError(
                    "delta removes an absent ontology annotation",
                    code="DELTA_ANNOTATION_REMOVE_ABSENT",
                )
            no_op_annotation_remove.append(annotation)
        else:
            annotation_remove.append(annotation)
    effective = OntologyDelta(
        CanonicalSet(add),
        CanonicalSet(remove),
        CanonicalSet(annotation_add),
        CanonicalSet(annotation_remove),
        metadata=requested.metadata,
        policy=DeltaPolicy.STRICT,
    )
    return (
        effective,
        CanonicalSet(no_op_add),
        CanonicalSet(no_op_remove),
        CanonicalSet(no_op_annotation_add),
        CanonicalSet(no_op_annotation_remove),
    )


def view_annotations(
    view: OntologyView,
    *,
    scope: AxiomScope = AxiomScope.CLOSURE,
    document_key: str | None = None,
) -> CanonicalSet[Annotation]:
    method = getattr(view, "ontology_annotations", None)
    if not callable(method):
        raise TypeError("OntologyView does not expose ontology annotations")
    result = method(scope=scope, document_key=document_key)
    if isinstance(result, CanonicalSet):
        return result
    return CanonicalSet(result)


def view_limits(view: OntologyView) -> ParseLimits:
    value = getattr(view, "limits", None)
    if isinstance(value, ParseLimits):
        return value
    options = getattr(view, "load_options", None)
    limits = getattr(options, "limits", None)
    return limits if isinstance(limits, ParseLimits) else ParseLimits()


def _known_effective_axiom_count(view: OntologyView) -> int | None:
    if isinstance(view, OntologySnapshot):
        return view.report.effective_axiom_count
    if isinstance(view, OntologyOverlay):
        base_size = _known_effective_axiom_count(view.base)
        if base_size is not None:
            return base_size + len(view.delta.add_axioms) - len(view.delta.remove_axioms)
    cached = getattr(view, "_axiom_count_cache", None)
    return cached if isinstance(cached, int) and not isinstance(cached, bool) else None


def canonical_merge(
    iterators: Iterable[Iterator[T]],
    *,
    excluded: CanonicalSet[StructuralNode] | CanonicalSet[AxiomNode] | None = None,
) -> Iterator[T]:
    """Merge canonical iterators, dropping duplicates and excluded values."""

    heap: list[tuple[bytes, int, T, Iterator[T]]] = []
    for ordinal, iterator in enumerate(iterators):
        try:
            value = next(iterator)
        except StopIteration:
            continue
        if not isinstance(value, StructuralNode):
            raise TypeError("canonical iterators must yield StructuralNode values")
        heapq.heappush(heap, (canonical_bytes(value), ordinal, value, iterator))
    previous: bytes | None = None
    while heap:
        key, ordinal, value, iterator = heapq.heappop(heap)
        if key != previous and (excluded is None or value not in excluded):
            yield value
            previous = key
        try:
            following = next(iterator)
        except StopIteration:
            continue
        if not isinstance(following, StructuralNode):
            raise TypeError("canonical iterators must yield StructuralNode values")
        heapq.heappush(
            heap,
            (canonical_bytes(following), ordinal, following, iterator),
        )


def collect_signature(
    roots: Iterable[StructuralNode],
    kind: EntityKind | None,
    *,
    include_builtins: bool,
) -> tuple[Entity, ...]:
    gathered: set[Entity] = set()
    for root in roots:
        gathered.update(node_signature(root))
    if not include_builtins:
        gathered = {item for item in gathered if not _is_builtin(item)}
    if kind is not None:
        gathered = {item for item in gathered if item.kind is kind}
    return tuple(sorted(gathered, key=canonical_bytes))


def _is_builtin(entity: Entity) -> bool:
    return entity.iri.value.startswith(
        (
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "http://www.w3.org/2000/01/rdf-schema#",
            "http://www.w3.org/2001/XMLSchema#",
            "http://www.w3.org/2002/07/owl#",
        )
    )


def _overlay_anchor_fingerprint(base: OntologyView) -> Fingerprint:
    if isinstance(base, OntologyOverlay):
        return base.structural_context.fingerprints[0]
    context = getattr(base, "structural_context", None)
    if isinstance(context, StructuralContext) and context.kind is StructuralContextKind.OVERLAY:
        return context.fingerprints[0]
    return base.structural_fingerprint


def _validate_axiom_type(value: type[A] | None) -> None:
    if value is not None and (not isinstance(value, type) or not issubclass(value, AxiomNode)):
        raise TypeError("axiom_type must be an axiom class or None")


def _require_view(value: object) -> None:
    if not _is_ontology_view(value):
        raise TypeError("base must implement OntologyView")


def _ensure_live(view: OntologyView) -> None:
    check = getattr(view, "_check_open", None)
    if callable(check):
        check()
        return
    # Foreign/future views may expose lifecycle validation only through report.
    _report = view.report


def _retain_dependency(view: OntologyView) -> object | None:
    method = getattr(view, "_retain_dependent", None)
    return method() if callable(method) else None


__all__ = ["OntologyOverlay", "apply_delta"]
