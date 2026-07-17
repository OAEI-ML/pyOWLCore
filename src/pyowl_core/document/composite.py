"""Zero-copy canonical composition of two or more ontology views."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from itertools import chain
from typing import TypeVar, cast

from pyowl_core._immutable import freeze_mapping
from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.exceptions import DeltaBaseMismatchError, DeltaError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import (
    IRI,
    Annotation,
    AnonymousIndividual,
    CanonicalSet,
    Entity,
    EntityKind,
    Literal,
    StructuralNode,
    canonical_bytes,
    encode_varint,
    structural_digest,
    walk,
)
from pyowl_core.model.axioms import AxiomNode

from .delta import DeltaPolicy, OntologyDelta
from .document import Fingerprint
from .fingerprint import (
    StructuralContext,
    StructuralContextKind,
    effective_structural_fingerprint,
    fingerprint_bytes,
    logical_fingerprint,
    signature_fingerprint,
)
from .overlay import (
    _SizedIterable,
    canonical_merge,
    collect_signature,
    view_annotations,
    view_limits,
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


@dataclass(frozen=True, slots=True)
class CompositeMember:
    view: OntologyView
    role: str | None = None

    def __post_init__(self) -> None:
        if not _is_ontology_view(self.view):
            raise TypeError("view must implement OntologyView")
        if self.role is None:
            return
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("role must be a nonempty string of at most 1024 UTF-8 bytes or None")
        try:
            size = len(self.role.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("role must contain valid UTF-8 text") from error
        if size > 1024:
            raise ValueError("role must be a nonempty string of at most 1024 UTF-8 bytes or None")


@dataclass(frozen=True, slots=True, eq=False, init=False)
class OntologyComposite:
    """A closure-only strong-reference union plus an optional bridge delta."""

    members: tuple[CompositeMember, ...]
    delta: OntologyDelta
    requested_delta: OntologyDelta = field(repr=False, compare=False)
    provenance_tree: tuple[CompositeMember, ...] = field(repr=False, compare=False)
    no_op_add_axioms: CanonicalSet[AxiomNode] = field(repr=False, compare=False)
    no_op_remove_axioms: CanonicalSet[AxiomNode] = field(repr=False, compare=False)
    no_op_add_ontology_annotations: CanonicalSet[Annotation] = field(repr=False, compare=False)
    no_op_remove_ontology_annotations: CanonicalSet[Annotation] = field(repr=False, compare=False)
    limits: ParseLimits = field(repr=False, compare=False)
    _sources: tuple[OntologyView, ...] = field(repr=False, compare=False)
    _roles: tuple[str | None, ...] = field(repr=False, compare=False)
    _capabilities: CoreCapabilities = field(repr=False, compare=False)
    _dependency_tokens: tuple[object | None, ...] = field(repr=False, compare=False)
    _context_cache: StructuralContext | None = field(repr=False, compare=False)
    _source_token_cache: tuple[bytes, ...] | None = field(repr=False, compare=False)
    _colliding_scope_cache: frozenset[bytes] | None = field(repr=False, compare=False)
    _scope_replacement_cache: tuple[Mapping[bytes, bytes], ...] | None = field(
        repr=False, compare=False
    )
    _provenance_digest_cache: bytes | None = field(repr=False, compare=False)
    _fingerprint_cache: tuple[Fingerprint, Fingerprint, Fingerprint] | None = field(
        repr=False, compare=False
    )
    _report_cache: LoadReport | None = field(repr=False, compare=False)
    _origin_cache: OriginIndex | None = field(repr=False, compare=False)
    _axiom_count_cache: int | None = field(repr=False, compare=False)
    _member_roles_cache: Mapping[str, str | None] | None = field(repr=False, compare=False)
    _cache_lock: threading.Lock = field(repr=False, compare=False)
    _index_cache: object = field(repr=False, compare=False)

    def __init__(
        self,
        views: Sequence[OntologyView],
        *,
        delta: OntologyDelta | None = None,
        roles: Sequence[str | None] | None = None,
    ) -> None:
        sources = tuple(views)
        if len(sources) < 2:
            raise ValueError("composition requires at least two member views")
        if not all(_is_ontology_view(item) for item in sources):
            raise TypeError("all composition members must implement OntologyView")
        if len({id(item) for item in sources}) != len(sources):
            raise DeltaError(
                "direct self-composition is not allowed",
                code="COMPOSITION_SELF_REFERENCE",
            )
        _reject_overlapping_composites(sources)
        selected_roles: tuple[str | None, ...] = (
            (None,) * len(sources) if roles is None else tuple(roles)
        )
        if len(selected_roles) != len(sources):
            raise ValueError("role count must match member count")
        top = tuple(
            CompositeMember(view, role) for view, role in zip(sources, selected_roles, strict=True)
        )
        flattened: list[CompositeMember] = []
        for member in top:
            if isinstance(member.view, OntologyComposite):
                flattened.extend(member.view.members)
            else:
                flattened.append(member)
        semantic_members: list[CompositeMember] = []
        for member in top:
            if isinstance(member.view, OntologyComposite) and _is_bridge_free(member.view):
                semantic_members.extend(member.view.members)
            else:
                semantic_members.append(member)
        semantic_sources = tuple(member.view for member in semantic_members)
        semantic_roles = tuple(member.role for member in semantic_members)
        limits = view_limits(sources[0])
        for source in sources[1:]:
            limits = limits.tightened_with(view_limits(source))
        limits.enforce("max_composite_members", len(flattened))
        selected_delta = OntologyDelta() if delta is None else delta
        if not isinstance(selected_delta, OntologyDelta):
            raise TypeError("delta must be OntologyDelta or None")
        limits.enforce("max_delta_entries", selected_delta.entry_count)
        for source in sources:
            _ensure_live(source)
        adapter = sources[0].capabilities.adapter_protocol
        model_schema = sources[0].capabilities.model_schema
        wire = sources[0].capabilities.wire_format
        if any(
            source.capabilities.adapter_protocol != adapter
            or source.capabilities.model_schema != model_schema
            or source.capabilities.wire_format != wire
            for source in sources[1:]
        ):
            raise DeltaError(
                "composition members have incompatible schemas",
                code="COMPOSITION_SCHEMA_MISMATCH",
            )
        backends = {source.capabilities.backend for source in sources}
        common_features = set.intersection(
            *(set(source.capabilities.features) for source in sources)
        )
        common_features.update({"ontology-composite", "zero-copy-view", "member-provenance"})
        capabilities = CoreCapabilities(
            adapter,
            model_schema,
            wire,
            frozenset(common_features),
            {},
            next(iter(backends)) if len(backends) == 1 else "mixed",
        )
        object.__setattr__(self, "members", tuple(flattened))
        object.__setattr__(self, "delta", OntologyDelta())
        object.__setattr__(self, "requested_delta", selected_delta)
        object.__setattr__(self, "provenance_tree", top)
        object.__setattr__(self, "no_op_add_axioms", CanonicalSet())
        object.__setattr__(self, "no_op_remove_axioms", CanonicalSet())
        object.__setattr__(self, "no_op_add_ontology_annotations", CanonicalSet())
        object.__setattr__(self, "no_op_remove_ontology_annotations", CanonicalSet())
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "_sources", semantic_sources)
        object.__setattr__(self, "_roles", semantic_roles)
        object.__setattr__(self, "_capabilities", capabilities)
        object.__setattr__(self, "_dependency_tokens", tuple(_retain(source) for source in sources))
        object.__setattr__(self, "_context_cache", None)
        object.__setattr__(self, "_source_token_cache", None)
        object.__setattr__(self, "_colliding_scope_cache", None)
        object.__setattr__(self, "_scope_replacement_cache", None)
        object.__setattr__(self, "_provenance_digest_cache", None)
        object.__setattr__(self, "_fingerprint_cache", None)
        object.__setattr__(self, "_report_cache", None)
        object.__setattr__(self, "_origin_cache", None)
        object.__setattr__(self, "_axiom_count_cache", None)
        object.__setattr__(self, "_member_roles_cache", None)
        object.__setattr__(self, "_cache_lock", threading.Lock())
        from pyowl_core.index.cache import create_index_cache

        object.__setattr__(self, "_index_cache", create_index_cache(limits))
        self._reject_ambiguous_anonymous_bridge(selected_delta)
        (
            effective,
            no_op_add,
            no_op_remove,
            no_op_annotation_add,
            no_op_annotation_remove,
        ) = self._validate_bridge(selected_delta)
        if not effective.is_empty and "owl2-dl-validated" in capabilities.features:
            features = set(capabilities.features)
            features.discard("owl2-dl-validated")
            capabilities = CoreCapabilities(
                capabilities.adapter_protocol,
                capabilities.model_schema,
                capabilities.wire_format,
                frozenset(features),
                capabilities.encoded_view_schemas,
                capabilities.backend,
            )
            object.__setattr__(self, "_capabilities", capabilities)
        object.__setattr__(self, "delta", effective)
        object.__setattr__(self, "no_op_add_axioms", no_op_add)
        object.__setattr__(self, "no_op_remove_axioms", no_op_remove)
        object.__setattr__(self, "no_op_add_ontology_annotations", no_op_annotation_add)
        object.__setattr__(self, "no_op_remove_ontology_annotations", no_op_annotation_remove)

    @property
    def capabilities(self) -> CoreCapabilities:
        self._ensure_members_live()
        return self._capabilities

    def _check_open(self) -> None:
        self._ensure_members_live()

    @property
    def is_complete(self) -> bool:
        self._ensure_members_live()
        return all(source.is_complete for source in self._sources)

    @property
    def structural_context(self) -> StructuralContext:
        retained = self._context_cache
        if retained is not None:
            return retained
        fingerprints: list[Fingerprint] = []
        for member in self.members:
            context = getattr(member.view, "structural_context", None)
            if (
                isinstance(context, StructuralContext)
                and context.kind is StructuralContextKind.COMPOSITE
            ):
                fingerprints.extend(context.fingerprints)
            else:
                fingerprints.append(member.view.structural_fingerprint)
        created = StructuralContext.composite(fingerprints)
        with self._cache_lock:
            retained = self._context_cache
            if retained is None:
                object.__setattr__(self, "_context_cache", created)
                return created
            return retained

    @property
    def composition_provenance_digest(self) -> bytes:
        retained = self._provenance_digest_cache
        if retained is not None:
            return retained
        pieces = [b"pyowl-core:composition-provenance:v1\x00"]
        for member in self.provenance_tree:
            pieces.append(fingerprint_bytes(member.view.structural_fingerprint))
            encoded = b"" if member.role is None else member.role.encode("utf-8")
            pieces.append(encode_varint(len(encoded)) + encoded)
        pieces.append(self.requested_delta.provenance_digest)
        created = hashlib.sha256(b"".join(pieces)).digest()
        with self._cache_lock:
            retained = self._provenance_digest_cache
            if retained is None:
                object.__setattr__(self, "_provenance_digest_cache", created)
                return created
            return retained

    @property
    def member_roles(self) -> Mapping[str, str | None]:
        retained = self._member_roles_cache
        if retained is not None:
            return retained
        created = freeze_mapping(
            {
                "member:" + token.hex(): role
                for token, role in zip(self._source_tokens(), self._roles, strict=True)
            }
        )
        with self._cache_lock:
            retained = self._member_roles_cache
            if retained is None:
                object.__setattr__(self, "_member_roles_cache", created)
                return created
            return retained

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
    def effective_axiom_count(self) -> int:
        retained = self._axiom_count_cache
        if retained is not None:
            return retained
        created = sum(1 for _ in self.iter_axioms())
        with self._cache_lock:
            retained = self._axiom_count_cache
            if retained is None:
                object.__setattr__(self, "_axiom_count_cache", created)
                return created
            return retained

    @property
    def report(self) -> LoadReport:
        self._ensure_members_live()
        retained = self._report_cache
        if retained is not None:
            return retained
        started = time.monotonic()
        structural, logical, signature_value = self._fingerprints()
        reports = tuple(source.report for source in self._sources)
        diagnostics = (
            tuple(diagnostic for report in reports for diagnostic in report.diagnostics)
            + self._no_op_diagnostics()
        )
        timings: dict[str, float] = {}
        for report in reports:
            for key, value in report.timings.items():
                timings[key] = timings.get(key, 0.0) + value
        timings["composite_fingerprint_seconds"] = time.monotonic() - started
        created = LoadReport(
            "mixed" if len({item.backend for item in reports}) > 1 else reports[0].backend,
            reports[0].api_version,
            reports[0].model_schema,
            sum(item.document_count for item in reports),
            sum(item.total_source_bytes for item in reports),
            self.effective_axiom_count,
            sum(item.resolution_attempts for item in reports),
            sum(item.acquisition_cache_hits for item in reports),
            sum(item.document_cache_hits for item in reports),
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
    def origin_index(self) -> OriginIndex:
        self._ensure_members_live()
        retained = self._origin_cache
        if retained is not None:
            return retained
        merged: dict[bytes, list[OriginOccurrence]] = {}
        observed = 0
        for source_index, source in enumerate(self._sources):
            token = self._source_tokens()[source_index]
            roots = chain(
                view_annotations(source),
                source.iter_axioms(),
                source.iter_extensions(),
            )
            for original in roots:
                moved = self._scope_value(source_index, original)
                if (
                    moved in self.delta.remove_axioms
                    or moved in self.delta.remove_ontology_annotations
                ):
                    continue
                origins = _source_origins(source, original)
                if not origins:
                    origins = (OriginOccurrence("unknown", 0),)
                prefixed = tuple(
                    OriginOccurrence(
                        "member:" + token.hex() + ":" + origin.document_key,
                        origin.occurrence,
                        origin.span,
                    )
                    for origin in origins
                )
                merged.setdefault(structural_digest(moved), []).extend(prefixed)
                observed += len(prefixed)
                self.limits.enforce("max_origin_entries", observed)
        bridge_key = "bridge:" + self.composition_provenance_digest.hex()
        bridge_values = chain(
            self.delta.add_axioms,
            self.delta.add_ontology_annotations,
        )
        for occurrence, value in enumerate(bridge_values):
            merged.setdefault(structural_digest(value), []).append(
                OriginOccurrence(bridge_key, occurrence)
            )
            observed += 1
            self.limits.enforce("max_origin_entries", observed)
        created = OriginIndex(
            {digest: tuple(sorted(set(occurrences))) for digest, occurrences in merged.items()}
        )
        with self._cache_lock:
            retained = self._origin_cache
            if retained is None:
                object.__setattr__(self, "_origin_cache", created)
                return created
            return retained

    def origins_for(self, value: StructuralNode) -> tuple[OriginOccurrence, ...]:
        if not isinstance(value, StructuralNode):
            raise TypeError("value must be StructuralNode")
        return self.origin_index.origins_for(value)

    def iter_axioms(
        self,
        axiom_type: type[A] | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[AxiomNode | A]:
        self._ensure_members_live()
        _validate_scope(scope, document_key)
        if axiom_type is not None and (
            not isinstance(axiom_type, type) or not issubclass(axiom_type, AxiomNode)
        ):
            raise TypeError("axiom_type must be an axiom class or None")
        member_iterators = tuple(
            self._member_axioms(index, source, axiom_type, scope)
            for index, source in enumerate(self._sources)
        )
        additions: Iterable[AxiomNode] = self.delta.add_axioms
        if axiom_type is not None:
            additions = (item for item in additions if type(item) is axiom_type)
        yield from cast(
            Iterator[AxiomNode | A],
            canonical_merge(
                (*member_iterators, iter(additions)),
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
        self._ensure_members_live()
        _validate_scope(scope, document_key)
        member_iterators = tuple(
            self._member_values(
                index,
                source,
                source.iter_extensions(namespace, scope=scope),
            )
            for index, source in enumerate(self._sources)
        )
        yield from canonical_merge(tuple(iter(item) for item in member_iterators))

    def ontology_annotations(
        self,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> CanonicalSet[Annotation]:
        self._ensure_members_live()
        _validate_scope(scope, document_key)
        values: list[Annotation] = []
        for index, source in enumerate(self._sources):
            values.extend(
                cast(Annotation, self._scope_value(index, item))
                for item in view_annotations(source, scope=scope)
            )
        return CanonicalSet(
            (
                *(item for item in values if item not in self.delta.remove_ontology_annotations),
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
        self._ensure_members_live()
        _validate_scope(scope, document_key)
        if not isinstance(axiom, AxiomNode):
            raise TypeError("axiom must be an OWL axiom")
        if axiom in self.delta.add_axioms:
            return True
        if axiom in self.delta.remove_axioms:
            return False
        return self._base_contains(axiom, scope=scope)

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
        if view_type is OntologyComposite or isinstance(self, view_type):
            if options:
                raise TypeError("OntologyComposite identity view accepts no options")
            return cast(V, self)
        from pyowl_core.index.cache import request_index_view

        return request_index_view(self, view_type, options)

    def materialize(self) -> OntologySnapshot:
        self._ensure_members_live()
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
            limits=self.limits,
            elapsed_seconds=time.monotonic() - started,
        )

    def _member_axioms(
        self,
        index: int,
        source: OntologyView,
        axiom_type: type[A] | None,
        scope: AxiomScope,
    ) -> Iterator[AxiomNode]:
        yield from cast(
            Iterator[AxiomNode],
            self._member_values(
                index,
                source,
                source.iter_axioms(axiom_type, scope=scope),
            ),
        )

    def _member_values(
        self,
        index: int,
        source: OntologyView,
        values: Iterable[StructuralNode],
    ) -> Iterator[StructuralNode]:
        mapped = (self._scope_value(index, value) for value in values)
        replacements = self._scope_replacements()
        if replacements is not None:
            reordered = bool(replacements[index])
        else:
            colliding = self._colliding_scopes()
            scopes = _known_anonymous_scopes(source)
            reordered = bool(colliding and (scopes is None or colliding & scopes))
        if reordered:
            # Replacing a document scope can change canonical byte order.  A
            # nested member still has to present a sorted stream to the heap
            # merge or equivalent nested/flat plans produce different bytes.
            yield from sorted(mapped, key=canonical_bytes)
            return
        yield from mapped

    def _base_contains(self, axiom: AxiomNode, *, scope: AxiomScope = AxiomScope.CLOSURE) -> bool:
        if not any(isinstance(item, AnonymousIndividual) for item in walk(axiom)):
            return any(source.contains(axiom, scope=scope) for source in self._sources)
        return any(value == axiom for value in self._base_axioms(scope=scope))

    def _base_axioms(self, *, scope: AxiomScope = AxiomScope.CLOSURE) -> Iterator[AxiomNode]:
        iterators = tuple(
            self._member_axioms(index, source, None, scope)
            for index, source in enumerate(self._sources)
        )
        yield from canonical_merge(iterators)

    def _base_annotations(self) -> CanonicalSet[Annotation]:
        values: list[Annotation] = []
        for index, source in enumerate(self._sources):
            values.extend(
                cast(Annotation, self._scope_value(index, item))
                for item in view_annotations(source)
            )
        return CanonicalSet(values)

    def _base_structural_fingerprint(self) -> Fingerprint:
        annotations = self._base_annotations()
        count = sum(1 for _ in self._base_axioms())
        extension_count = sum(1 for _ in self._base_extensions())
        return effective_structural_fingerprint(
            self.structural_context,
            annotations,
            _SizedIterable(count, lambda: self._base_axioms()),
            _SizedIterable(extension_count, lambda: self._base_extensions()),
        )

    def _base_extensions(self) -> Iterator[StructuralNode]:
        iterators = tuple(
            self._member_values(index, source, source.iter_extensions())
            for index, source in enumerate(self._sources)
        )
        yield from canonical_merge(tuple(iter(item) for item in iterators))

    def _validate_bridge(
        self, requested: OntologyDelta
    ) -> tuple[
        OntologyDelta,
        CanonicalSet[AxiomNode],
        CanonicalSet[AxiomNode],
        CanonicalSet[Annotation],
        CanonicalSet[Annotation],
    ]:
        if requested.expected_base_fingerprint is None and requested.is_empty:
            return (
                requested,
                CanonicalSet(),
                CanonicalSet(),
                CanonicalSet(),
                CanonicalSet(),
            )
        expected = requested.expected_base_fingerprint
        if expected is not None and expected != self._base_structural_fingerprint():
            raise DeltaBaseMismatchError(
                "bridge delta expected-base fingerprint does not match the composition",
                code="DELTA_BASE_MISMATCH",
            )
        add: list[AxiomNode] = []
        remove: list[AxiomNode] = []
        no_op_add: list[AxiomNode] = []
        no_op_remove: list[AxiomNode] = []
        for item in requested.add_axioms:
            if self._base_contains(item):
                if requested.policy is DeltaPolicy.STRICT:
                    raise DeltaError("bridge adds an existing axiom", code="DELTA_ADD_EXISTS")
                no_op_add.append(item)
            else:
                add.append(item)
        for item in requested.remove_axioms:
            if not self._base_contains(item):
                if requested.policy is DeltaPolicy.STRICT:
                    raise DeltaError("bridge removes an absent axiom", code="DELTA_REMOVE_ABSENT")
                no_op_remove.append(item)
            else:
                remove.append(item)
        base_annotations = self._base_annotations()
        annotation_add: list[Annotation] = []
        annotation_remove: list[Annotation] = []
        no_op_annotation_add: list[Annotation] = []
        no_op_annotation_remove: list[Annotation] = []
        for annotation in requested.add_ontology_annotations:
            if annotation in base_annotations:
                if requested.policy is DeltaPolicy.STRICT:
                    raise DeltaError(
                        "bridge adds an existing ontology annotation",
                        code="DELTA_ANNOTATION_ADD_EXISTS",
                    )
                no_op_annotation_add.append(annotation)
            else:
                annotation_add.append(annotation)
        for annotation in requested.remove_ontology_annotations:
            if annotation not in base_annotations:
                if requested.policy is DeltaPolicy.STRICT:
                    raise DeltaError(
                        "bridge removes an absent ontology annotation",
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

    def _reject_ambiguous_anonymous_bridge(self, requested: OntologyDelta) -> None:
        requested_scopes = {
            item.document_scope
            for value in chain(
                requested.add_axioms,
                requested.remove_axioms,
                requested.add_ontology_annotations,
                requested.remove_ontology_annotations,
            )
            for item in walk(value)
            if isinstance(item, AnonymousIndividual)
        }
        if not requested_scopes:
            return
        counts: dict[bytes, int] = {}
        for source in self._sources:
            lineage = _known_anonymous_lineage(source)
            if lineage is None:
                continue
            for _current, original, _leaf in lineage:
                counts[original] = counts.get(original, 0) + 1
        if any(counts.get(scope, 0) > 1 for scope in requested_scopes):
            raise DeltaError(
                "anonymous bridge scope identifies more than one composition member; "
                "compose first and apply a delta using the composed identity",
                code="COMPOSITION_ANONYMOUS_BRIDGE_AMBIGUOUS",
            )

    def _source_tokens(self) -> tuple[bytes, ...]:
        retained = self._source_token_cache
        if retained is not None:
            return retained
        keyed = sorted(
            (
                fingerprint_bytes(source.structural_fingerprint),
                index,
            )
            for index, source in enumerate(self._sources)
        )
        counters: dict[bytes, int] = {}
        tokens: list[bytes] = [b""] * len(self._sources)
        for encoded, index in keyed:
            ordinal = counters.get(encoded, 0)
            counters[encoded] = ordinal + 1
            tokens[index] = hashlib.sha256(
                b"pyowl-core:composition-member:v1\x00" + encoded + encode_varint(ordinal)
            ).digest()
        created = tuple(tokens)
        with self._cache_lock:
            retained = self._source_token_cache
            if retained is None:
                object.__setattr__(self, "_source_token_cache", created)
                return created
            return retained

    def _colliding_scopes(self) -> frozenset[bytes]:
        retained = self._colliding_scope_cache
        if retained is not None:
            return retained
        counts: dict[bytes, int] = {}
        known_scopes = tuple(_known_anonymous_scopes(source) for source in self._sources)
        if all(scopes is not None for scopes in known_scopes):
            source_scope_sets = cast(tuple[frozenset[bytes], ...], known_scopes)
        else:
            scanned: list[frozenset[bytes]] = []
            for source in self._sources:
                scanned_scopes: set[bytes] = set()
                collections: tuple[Iterable[StructuralNode], ...] = (
                    view_annotations(source),
                    source.iter_axioms(),
                    source.iter_extensions(),
                )
                for collection in collections:
                    for value in collection:
                        scanned_scopes.update(
                            item.document_scope
                            for item in walk(value)
                            if isinstance(item, AnonymousIndividual)
                        )
                scanned.append(frozenset(scanned_scopes))
            source_scope_sets = tuple(scanned)
        for scopes_for_source in source_scope_sets:
            for scope in scopes_for_source:
                counts[scope] = counts.get(scope, 0) + 1
        created = frozenset(scope for scope, count in counts.items() if count > 1)
        with self._cache_lock:
            retained = self._colliding_scope_cache
            if retained is None:
                object.__setattr__(self, "_colliding_scope_cache", created)
                return created
            return retained

    def _anonymous_document_scopes(self) -> frozenset[bytes] | None:
        lineage = self._anonymous_scope_lineage()
        if lineage is None:
            return None
        return frozenset(current for current, _original, _leaf in lineage)

    def _anonymous_scope_lineage(self) -> tuple[tuple[bytes, bytes, bytes], ...] | None:
        if self._bridge_changes_anonymous():
            return None
        source_lineages = tuple(_known_anonymous_lineage(source) for source in self._sources)
        if any(lineage is None for lineage in source_lineages):
            return None
        replacements = self._scope_replacements()
        if replacements is None:
            return None
        return tuple(
            (
                replacements[index].get(current, current),
                original,
                leaf,
            )
            for index, optional_lineage in enumerate(source_lineages)
            for current, original, leaf in cast(
                tuple[tuple[bytes, bytes, bytes], ...], optional_lineage
            )
        )

    def _scope_replacements(self) -> tuple[Mapping[bytes, bytes], ...] | None:
        retained = self._scope_replacement_cache
        if retained is not None:
            return retained
        source_lineages = tuple(_known_anonymous_lineage(source) for source in self._sources)
        if any(lineage is None for lineage in source_lineages):
            return None
        grouped: dict[bytes, list[tuple[bytes, bytes, int]]] = {}
        for index, optional_lineage in enumerate(source_lineages):
            for current, original, leaf in cast(
                tuple[tuple[bytes, bytes, bytes], ...], optional_lineage
            ):
                grouped.setdefault(original, []).append((leaf, current, index))
        replacements: list[dict[bytes, bytes]] = [dict() for _ in self._sources]
        for original, entries in grouped.items():
            if len(entries) < 2:
                continue
            counters: dict[bytes, int] = {}
            for leaf, current, index in sorted(entries):
                ordinal = counters.get(leaf, 0)
                counters[leaf] = ordinal + 1
                token = hashlib.sha256(
                    b"pyowl-core:composition-member:v1\x00" + leaf + encode_varint(ordinal)
                ).digest()
                target = _scoped_anonymous_digest(token, original)
                if target != current:
                    replacements[index][current] = target
        created = tuple(freeze_mapping(values) for values in replacements)
        with self._cache_lock:
            retained = self._scope_replacement_cache
            if retained is None:
                object.__setattr__(self, "_scope_replacement_cache", created)
                return created
            return retained

    def _bridge_changes_anonymous(self) -> bool:
        values = chain(
            self.delta.add_axioms,
            self.delta.remove_axioms,
            self.delta.add_ontology_annotations,
            self.delta.remove_ontology_annotations,
        )
        return any(
            isinstance(item, AnonymousIndividual) for value in values for item in walk(value)
        )

    def _scope_value(self, index: int, value: StructuralNode) -> StructuralNode:
        replacements = self._scope_replacements()
        if replacements is not None:
            return _map_anonymous_scopes(value, replacements[index])
        colliding_scopes = self._colliding_scopes()
        if not colliding_scopes or not any(
            isinstance(item, AnonymousIndividual) and item.document_scope in colliding_scopes
            for item in walk(value)
        ):
            return value
        return _scope_member_value(
            value,
            self._source_tokens()[index],
            colliding_scopes,
        )

    def _fingerprints(self) -> tuple[Fingerprint, Fingerprint, Fingerprint]:
        self._ensure_members_live()
        retained = self._fingerprint_cache
        if retained is not None:
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
        additions = len(self.no_op_add_axioms) + len(self.no_op_add_ontology_annotations)
        removals = len(self.no_op_remove_axioms) + len(self.no_op_remove_ontology_annotations)
        if additions:
            values.append(
                Diagnostic(
                    "DELTA_IDEMPOTENT_ADD_NOOP",
                    Severity.INFO,
                    "idempotent bridge additions were already present",
                    details={"count": additions},
                )
            )
        if removals:
            values.append(
                Diagnostic(
                    "DELTA_IDEMPOTENT_REMOVE_NOOP",
                    Severity.INFO,
                    "idempotent bridge removals were already absent",
                    details={"count": removals},
                )
            )
        return tuple(values)

    def _ensure_members_live(self) -> None:
        for member in self.provenance_tree:
            _ensure_live(member.view)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OntologyComposite):
            return NotImplemented
        return self.structural_fingerprint == other.structural_fingerprint

    def __hash__(self) -> int:
        value = int.from_bytes(self.structural_fingerprint.digest[:8], "big", signed=True)
        return -2 if value == -1 else value


def compose_views(
    *views: OntologyView,
    delta: OntologyDelta | None = None,
    roles: Sequence[str | None] | None = None,
) -> OntologyComposite:
    """Compose views without cloning, flattening, serializing, or reparsing them."""

    return OntologyComposite(views, delta=delta, roles=roles)


def _scope_member_value(
    value: StructuralNode,
    token: bytes,
    colliding_scopes: frozenset[bytes],
) -> StructuralNode:
    if not any(
        isinstance(item, AnonymousIndividual) and item.document_scope in colliding_scopes
        for item in walk(value)
    ):
        return value
    replacements: dict[AnonymousIndividual, AnonymousIndividual] = {}
    return cast(
        StructuralNode,
        _replace_anonymous(value, token, colliding_scopes, replacements),
    )


def _map_anonymous_scopes(
    value: StructuralNode,
    scopes: Mapping[bytes, bytes],
) -> StructuralNode:
    if not scopes or not any(
        isinstance(item, AnonymousIndividual) and item.document_scope in scopes
        for item in walk(value)
    ):
        return value
    replacements: dict[AnonymousIndividual, AnonymousIndividual] = {}
    return cast(StructuralNode, _replace_mapped_anonymous(value, scopes, replacements))


def _replace_mapped_anonymous(
    value: object,
    scopes: Mapping[bytes, bytes],
    replacements: dict[AnonymousIndividual, AnonymousIndividual],
) -> object:
    if isinstance(value, AnonymousIndividual):
        target = scopes.get(value.document_scope)
        if target is None:
            return value
        retained = replacements.get(value)
        if retained is None:
            # Composition standardizes the member scope while retaining the
            # leaf's alpha-canonical local key, making nested/flat plans equal.
            retained = AnonymousIndividual(target, value.local_key)
            replacements[value] = retained
        return retained
    if isinstance(value, CanonicalSet):
        return CanonicalSet(
            cast(StructuralNode, _replace_mapped_anonymous(item, scopes, replacements))
            for item in value
        )
    if isinstance(value, tuple):
        return tuple(_replace_mapped_anonymous(item, scopes, replacements) for item in value)
    if not isinstance(value, StructuralNode) or isinstance(value, (IRI, Entity, Literal)):
        return value
    if not is_dataclass(value):
        return value
    return type(value)(
        **{
            item.name: _replace_mapped_anonymous(getattr(value, item.name), scopes, replacements)
            for item in fields(value)
        }
    )


def _replace_anonymous(
    value: object,
    token: bytes,
    colliding_scopes: frozenset[bytes],
    replacements: dict[AnonymousIndividual, AnonymousIndividual],
) -> object:
    if isinstance(value, AnonymousIndividual):
        if value.document_scope not in colliding_scopes:
            return value
        retained = replacements.get(value)
        if retained is None:
            scope = _scoped_anonymous_digest(token, value.document_scope)
            # See _replace_mapped_anonymous: only the member scope changes.
            retained = AnonymousIndividual(scope, value.local_key)
            replacements[value] = retained
        return retained
    if isinstance(value, CanonicalSet):
        return CanonicalSet(
            cast(
                StructuralNode,
                _replace_anonymous(item, token, colliding_scopes, replacements),
            )
            for item in value
        )
    if isinstance(value, tuple):
        return tuple(
            _replace_anonymous(item, token, colliding_scopes, replacements) for item in value
        )
    if not isinstance(value, StructuralNode) or isinstance(value, (IRI, Entity, Literal)):
        return value
    if not is_dataclass(value):
        return value
    return type(value)(
        **{
            item.name: _replace_anonymous(
                getattr(value, item.name), token, colliding_scopes, replacements
            )
            for item in fields(value)
        }
    )


def _source_origins(source: OntologyView, value: StructuralNode) -> tuple[OriginOccurrence, ...]:
    method = getattr(source, "origins_for", None)
    if callable(method):
        return tuple(method(value))
    return source.origin_index.origins_for(value)


def _known_anonymous_scopes(source: OntologyView) -> frozenset[bytes] | None:
    method = getattr(source, "_anonymous_document_scopes", None)
    if not callable(method):
        return None
    result = method()
    if not isinstance(result, frozenset) or not all(
        isinstance(scope, bytes) and len(scope) == 32 for scope in result
    ):
        return None
    return result


def _known_anonymous_lineage(
    source: OntologyView,
) -> tuple[tuple[bytes, bytes, bytes], ...] | None:
    method = getattr(source, "_anonymous_scope_lineage", None)
    if not callable(method):
        return None
    result = method()
    if not isinstance(result, tuple) or not all(
        isinstance(entry, tuple)
        and len(entry) == 3
        and all(isinstance(value, bytes) and len(value) > 0 for value in entry)
        and len(entry[0]) == len(entry[1]) == 32
        for entry in result
    ):
        return None
    return result


def _scoped_anonymous_digest(token: bytes, scope: bytes) -> bytes:
    return hashlib.sha256(b"pyowl-core:composition-anonymous-scope:v1\x00" + token + scope).digest()


def _validate_scope(scope: AxiomScope, document_key: str | None) -> None:
    if not isinstance(scope, AxiomScope):
        raise TypeError("scope must be AxiomScope")
    if scope is not AxiomScope.CLOSURE:
        raise ValueError("composite views support CLOSURE scope only")
    if document_key is not None:
        raise ValueError(f"document_key is not valid for composite {scope.value} scope")


def _reject_overlapping_composites(sources: tuple[OntologyView, ...]) -> None:
    observed: set[int] = set()

    def visit(value: OntologyView) -> None:
        identity = id(value)
        if identity in observed:
            raise DeltaError(
                "composition contains a recursive or overlapping member",
                code="COMPOSITION_CYCLE",
            )
        observed.add(identity)
        if isinstance(value, OntologyComposite):
            for member in value.provenance_tree:
                visit(member.view)

    for source in sources:
        visit(source)


def _is_bridge_free(value: OntologyComposite) -> bool:
    if not value.delta.is_empty:
        return False
    return all(
        not isinstance(member.view, OntologyComposite) or _is_bridge_free(member.view)
        for member in value.provenance_tree
    )


def _retain(view: OntologyView) -> object | None:
    method = getattr(view, "_retain_dependent", None)
    return method() if callable(method) else None


def _ensure_live(view: OntologyView) -> None:
    check = getattr(view, "_check_open", None)
    if callable(check):
        check()
        return
    _report = view.report


__all__ = ["CompositeMember", "OntologyComposite", "compose_views"]
