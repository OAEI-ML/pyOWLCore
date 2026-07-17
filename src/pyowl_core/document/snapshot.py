"""Concrete immutable resolved ontology snapshot and read-only view protocol."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from typing import Protocol, TypeAlias, TypeGuard, TypeVar, cast, runtime_checkable

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.config import LoadOptions
from pyowl_core.diagnostics import Diagnostic
from pyowl_core.exceptions import ProfileError, ResourceLimitError
from pyowl_core.io.source import DocumentSource
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
    re_scope_anonymous,
    structural_digest,
    validate_owl2_dl,
)
from pyowl_core.model import (
    signature as node_signature,
)
from pyowl_core.model.axioms import AxiomNode
from pyowl_core.model.validation import OWL2DLReport

from .document import Fingerprint, OntologyDocument
from .fingerprint import (
    StructuralContext,
    effective_structural_fingerprint,
    fingerprint_bytes,
    logical_fingerprint,
    signature_fingerprint,
    snapshot_structural_fingerprint,
)
from .imports import DocumentRecord, ImportManifest
from .provenance import OriginIndex, OriginOccurrence

A = TypeVar("A", bound=AxiomNode)
V = TypeVar("V")


class AxiomScope(str, Enum):
    ROOT = "root"
    CLOSURE = "closure"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class CoreCapabilities:
    adapter_protocol: int
    model_schema: int
    wire_format: tuple[int, int]
    features: frozenset[str]
    encoded_view_schemas: Mapping[str, int] = field(default_factory=FrozenMap)
    backend: str = "python"

    def __post_init__(self) -> None:
        if isinstance(self.adapter_protocol, bool) or not isinstance(self.adapter_protocol, int):
            raise TypeError("adapter_protocol must be int")
        if isinstance(self.model_schema, bool) or not isinstance(self.model_schema, int):
            raise TypeError("model_schema must be int")
        if (
            not isinstance(self.wire_format, tuple)
            or len(self.wire_format) != 2
            or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in self.wire_format
            )
        ):
            raise TypeError("wire_format must be a pair of integers")
        features = frozenset(self.features)
        if not all(isinstance(item, str) and item for item in features):
            raise TypeError("features must contain nonempty strings")
        schemas: dict[str, int] = {}
        for key, value in self.encoded_view_schemas.items():
            if not isinstance(key, str) or not key:
                raise TypeError("encoded view schema names must be nonempty strings")
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("encoded view schemas must be positive integers")
            schemas[key] = value
        if not isinstance(self.backend, str) or not self.backend:
            raise ValueError("backend must be a nonempty string")
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "encoded_view_schemas", freeze_mapping(schemas))


@dataclass(frozen=True, slots=True)
class LoadReport:
    backend: str
    api_version: tuple[int, int]
    model_schema: int
    document_count: int
    total_source_bytes: int
    effective_axiom_count: int
    resolution_attempts: int
    acquisition_cache_hits: int
    document_cache_hits: int
    timings: Mapping[str, float]
    diagnostics: tuple[Diagnostic, ...]
    structural_fingerprint: Fingerprint
    logical_fingerprint: Fingerprint
    signature_fingerprint: Fingerprint
    owl2_dl_report: OWL2DLReport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.backend, str) or not self.backend:
            raise ValueError("backend must be a nonempty string")
        for name in (
            "document_count",
            "total_source_bytes",
            "effective_axiom_count",
            "resolution_attempts",
            "acquisition_cache_hits",
            "document_cache_hits",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        timings: dict[str, float] = {}
        for key, value in self.timings.items():
            if not isinstance(key, str) or not key or not isinstance(value, (int, float)):
                raise TypeError("timings must map nonempty strings to numbers")
            if value < 0:
                raise ValueError("timings must be nonnegative")
            timings[key] = float(value)
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(item, Diagnostic) for item in diagnostics):
            raise TypeError("diagnostics must contain Diagnostic values")
        object.__setattr__(self, "timings", freeze_mapping(timings))
        object.__setattr__(self, "diagnostics", diagnostics)
        if self.owl2_dl_report is not None and not isinstance(self.owl2_dl_report, OWL2DLReport):
            raise TypeError("owl2_dl_report must be OWL2DLReport or None")


@runtime_checkable
class OntologyView(Protocol):
    @property
    def capabilities(self) -> CoreCapabilities: ...

    def iter_axioms(
        self,
        axiom_type: type[A] | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[AxiomNode | A]: ...

    def iter_extensions(
        self,
        namespace: str | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[StructuralNode]: ...

    def contains(
        self,
        axiom: AxiomNode,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> bool: ...

    def ontology_annotations(
        self,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> CanonicalSet[Annotation]: ...

    def signature(
        self,
        kind: EntityKind | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
        include_builtins: bool = True,
    ) -> tuple[Entity, ...]: ...

    def view(self, view_type: type[V], /, **options: object) -> V: ...

    @property
    def origin_index(self) -> OriginIndex: ...

    @property
    def is_complete(self) -> bool: ...

    @property
    def structural_fingerprint(self) -> Fingerprint: ...

    @property
    def logical_fingerprint(self) -> Fingerprint: ...

    @property
    def signature_fingerprint(self) -> Fingerprint: ...

    @property
    def report(self) -> LoadReport: ...


@runtime_checkable
class SnapshotProvider(Protocol):
    def owl_snapshot(self) -> OntologyView: ...


_ONTOLOGY_VIEW_ATTRIBUTES = (
    "capabilities",
    "iter_axioms",
    "iter_extensions",
    "contains",
    "ontology_annotations",
    "signature",
    "view",
    "origin_index",
    "is_complete",
    "structural_fingerprint",
    "logical_fingerprint",
    "signature_fingerprint",
    "report",
)
_MISSING_VIEW_ATTRIBUTE = object()


def _is_ontology_view(value: object) -> TypeGuard[OntologyView]:
    """Check the runtime protocol without invoking descriptors on Python 3.10."""

    return all(
        inspect.getattr_static(value, name, _MISSING_VIEW_ATTRIBUTE) is not _MISSING_VIEW_ATTRIBUTE
        for name in _ONTOLOGY_VIEW_ATTRIBUTES
    )


OntologyInput: TypeAlias = DocumentSource | OntologyDocument | OntologyView | SnapshotProvider


@dataclass(frozen=True, slots=True, eq=False)
class OntologySnapshot:
    """Materialized immutable document closure implementing OntologyView."""

    root: OntologyDocument
    documents: tuple[OntologyDocument, ...]
    import_manifest: ImportManifest
    root_document_key: str
    load_options: LoadOptions
    diagnostics: tuple[Diagnostic, ...] = ()
    timings: Mapping[str, float] = field(default_factory=FrozenMap, repr=False, compare=False)
    resolution_attempts: int = field(default=0, repr=False, compare=False)
    acquisition_cache_hits: int = field(default=0, repr=False, compare=False)
    document_cache_hits: int = field(default=0, repr=False, compare=False)
    _document_by_key: Mapping[str, OntologyDocument] = field(init=False, repr=False, compare=False)
    _axioms_by_key: Mapping[str, CanonicalSet[AxiomNode]] = field(
        init=False, repr=False, compare=False
    )
    _annotations_by_key: Mapping[str, CanonicalSet[Annotation]] = field(
        init=False, repr=False, compare=False
    )
    _extensions_by_key: Mapping[str, CanonicalSet[StructuralNode]] = field(
        init=False, repr=False, compare=False
    )
    _closure_axioms: CanonicalSet[AxiomNode] = field(init=False, repr=False, compare=False)
    _closure_annotations: CanonicalSet[Annotation] = field(init=False, repr=False, compare=False)
    _closure_extensions: CanonicalSet[StructuralNode] = field(init=False, repr=False, compare=False)
    _anonymous_scopes: frozenset[bytes] = field(init=False, repr=False, compare=False)
    _origin_index: OriginIndex = field(init=False, repr=False, compare=False)
    _capabilities: CoreCapabilities = field(init=False, repr=False, compare=False)
    _structural_fingerprint: Fingerprint = field(init=False, repr=False, compare=False)
    _logical_fingerprint: Fingerprint = field(init=False, repr=False, compare=False)
    _signature_fingerprint: Fingerprint = field(init=False, repr=False, compare=False)
    _owl2_dl_report: OWL2DLReport | None = field(init=False, repr=False, compare=False)
    _report: LoadReport = field(init=False, repr=False, compare=False)
    _preserve_document_scopes: bool = field(default=False, repr=False, compare=False)
    _origin_index_override: OriginIndex | None = field(default=None, repr=False, compare=False)
    _structural_context: StructuralContext | None = field(default=None, repr=False, compare=False)
    _structural_fingerprint_override: Fingerprint | None = field(
        default=None, repr=False, compare=False
    )
    _complete_override: bool | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.root, OntologyDocument):
            raise TypeError("root must be OntologyDocument")
        documents = tuple(self.documents)
        if not all(isinstance(item, OntologyDocument) for item in documents):
            raise TypeError("documents must contain OntologyDocument values")
        if not isinstance(self.import_manifest, ImportManifest):
            raise TypeError("import_manifest must be ImportManifest")
        if len(documents) != len(self.import_manifest.documents):
            raise ValueError("documents must align with manifest records")
        if not isinstance(self.load_options, LoadOptions):
            raise TypeError("load_options must be LoadOptions")
        records = self.import_manifest.documents
        document_by_key = {
            record.document_key: document
            for record, document in zip(records, documents, strict=True)
        }
        if document_by_key.get(self.root_document_key) is not self.root:
            raise ValueError("root_document_key must identify the exact root document")
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(item, Diagnostic) for item in diagnostics):
            raise TypeError("diagnostics must contain Diagnostic values")
        if not isinstance(self._preserve_document_scopes, bool):
            raise TypeError("_preserve_document_scopes must be bool")
        if self._origin_index_override is not None and not isinstance(
            self._origin_index_override, OriginIndex
        ):
            raise TypeError("_origin_index_override must be OriginIndex or None")
        if self._structural_context is not None and not isinstance(
            self._structural_context, StructuralContext
        ):
            raise TypeError("_structural_context must be StructuralContext or None")
        if self._structural_fingerprint_override is not None and not isinstance(
            self._structural_fingerprint_override, Fingerprint
        ):
            raise TypeError("_structural_fingerprint_override must be Fingerprint or None")
        if self._complete_override is not None and not isinstance(self._complete_override, bool):
            raise TypeError("_complete_override must be bool or None")
        scoped = (
            _preserved_documents(records, documents)
            if self._preserve_document_scopes
            else _scope_documents(records, documents)
        )
        axioms_by_key = {key: value.axioms for key, value in scoped.items()}
        annotations_by_key = {key: value.annotations for key, value in scoped.items()}
        extensions_by_key = {key: value.extensions for key, value in scoped.items()}
        if len(scoped) == 1:
            only = next(iter(scoped.values()))
            closure_axioms = only.axioms
            closure_extensions = only.extensions
            closure_annotations = only.annotations
        else:
            closure_axioms = CanonicalSet(
                axiom for values in axioms_by_key.values() for axiom in values
            )
            closure_extensions = CanonicalSet(
                extension for values in extensions_by_key.values() for extension in values
            )
            closure_annotations = CanonicalSet(
                annotation for values in annotations_by_key.values() for annotation in values
            )
        origin_index = self._origin_index_override or _merge_origins(
            records,
            documents,
            scoped,
            maximum=self.load_options.limits.max_origin_entries,
        )
        owl2_dl_report: OWL2DLReport | None = None
        if self.load_options.validate_owl2_dl:
            if not self.import_manifest.is_complete:
                raise ProfileError(
                    "OWL 2 DL validation requires a complete import closure",
                    code="OWL2DL_INCOMPLETE_CLOSURE",
                )
            owl2_dl_report = replace(validate_owl2_dl(closure_axioms), complete=True)
            if not owl2_dl_report.conforms:
                codes = ", ".join(issue.code for issue in owl2_dl_report.issues)
                raise ProfileError(
                    f"OWL 2 DL validation failed: {codes}",
                    code="OWL2DL_VALIDATION_FAILED",
                )
        capabilities = CoreCapabilities(
            1,
            1,
            (1, 0),
            frozenset(
                {
                    "owl2-structural",
                    "document-boundaries",
                    "import-manifest",
                    "immutable-snapshot",
                    "document-scoped-anonymous",
                }
                | ({"materialized-view"} if self._structural_context is not None else set())
                | (
                    {"source-map"}
                    if all(item.source_map is not None for item in documents)
                    else set()
                )
                | ({"owl2-dl-validated"} if owl2_dl_report is not None else set())
            ),
            {},
            "python",
        )
        if self._structural_fingerprint_override is not None:
            structural = self._structural_fingerprint_override
        elif self._structural_context is None:
            structural = snapshot_structural_fingerprint(
                self.import_manifest,
                (
                    (
                        record.document_key,
                        scoped[record.document_key].annotations,
                        scoped[record.document_key].axioms,
                        scoped[record.document_key].extensions,
                    )
                    for record in self.import_manifest.documents
                ),
            )
        else:
            structural = effective_structural_fingerprint(
                self._structural_context,
                closure_annotations,
                closure_axioms,
                closure_extensions,
            )
        logical = logical_fingerprint(closure_axioms, closure_extensions)
        signature_values = _signature(
            (*closure_annotations, *closure_axioms, *closure_extensions),
            None,
            include_builtins=True,
        )
        signature_value = signature_fingerprint(signature_values, include_builtins=True)
        report = LoadReport(
            "python",
            (0, 1),
            1,
            len(documents),
            sum(item.provenance.byte_length for item in documents),
            len(closure_axioms),
            self.resolution_attempts,
            self.acquisition_cache_hits,
            self.document_cache_hits,
            self.timings,
            diagnostics,
            structural,
            logical,
            signature_value,
            owl2_dl_report,
        )
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "timings", freeze_mapping(self.timings))
        object.__setattr__(self, "_document_by_key", freeze_mapping(document_by_key))
        object.__setattr__(self, "_axioms_by_key", freeze_mapping(axioms_by_key))
        object.__setattr__(self, "_annotations_by_key", freeze_mapping(annotations_by_key))
        object.__setattr__(self, "_extensions_by_key", freeze_mapping(extensions_by_key))
        object.__setattr__(self, "_closure_axioms", closure_axioms)
        object.__setattr__(self, "_closure_annotations", closure_annotations)
        object.__setattr__(self, "_closure_extensions", closure_extensions)
        object.__setattr__(
            self,
            "_anonymous_scopes",
            frozenset(scope for value in scoped.values() for scope in value.anonymous_scopes),
        )
        object.__setattr__(self, "_origin_index", origin_index)
        object.__setattr__(self, "_capabilities", capabilities)
        object.__setattr__(self, "_structural_fingerprint", structural)
        object.__setattr__(self, "_logical_fingerprint", logical)
        object.__setattr__(self, "_signature_fingerprint", signature_value)
        object.__setattr__(self, "_owl2_dl_report", owl2_dl_report)
        object.__setattr__(self, "_report", report)

    @property
    def capabilities(self) -> CoreCapabilities:
        return self._capabilities

    def _check_open(self) -> None:
        """Lifecycle hook shared with future mapped snapshots."""

    def _anonymous_document_scopes(self) -> frozenset[bytes]:
        return self._anonymous_scopes

    def _anonymous_scope_lineage(self) -> tuple[tuple[bytes, bytes, bytes], ...]:
        leaf = fingerprint_bytes(self.structural_fingerprint)
        return tuple((scope, scope, leaf) for scope in sorted(self._anonymous_scopes))

    @property
    def is_complete(self) -> bool:
        if self._complete_override is not None:
            return self._complete_override
        return self.import_manifest.is_complete

    @property
    def origin_index(self) -> OriginIndex:
        return self._origin_index

    @property
    def structural_context(self) -> StructuralContext | None:
        return self._structural_context

    @property
    def structural_fingerprint(self) -> Fingerprint:
        return self._structural_fingerprint

    @property
    def logical_fingerprint(self) -> Fingerprint:
        return self._logical_fingerprint

    @property
    def signature_fingerprint(self) -> Fingerprint:
        return self._signature_fingerprint

    @property
    def report(self) -> LoadReport:
        return self._report

    @property
    def owl2_dl_report(self) -> OWL2DLReport | None:
        return self._owl2_dl_report

    def document(self, document_key: str) -> OntologyDocument:
        if not isinstance(document_key, str) or not document_key:
            raise ValueError("document_key must be a nonempty string")
        return self._document_by_key[document_key]

    def iter_documents(self) -> Iterator[tuple[DocumentRecord, OntologyDocument]]:
        yield from zip(self.import_manifest.documents, self.documents, strict=True)

    def iter_axioms(
        self,
        axiom_type: type[A] | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[AxiomNode | A]:
        values = self._axioms(scope, document_key)
        if axiom_type is None:
            yield from values
            return
        if not isinstance(axiom_type, type) or not issubclass(axiom_type, AxiomNode):
            raise TypeError("axiom_type must be an axiom class or None")
        yield from cast(Iterator[A], (item for item in values if type(item) is axiom_type))

    def iter_extensions(
        self,
        namespace: str | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[StructuralNode]:
        if namespace not in {None, "swrl"}:
            return
        yield from self._extensions(scope, document_key)

    def ontology_annotations(
        self,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> CanonicalSet[Annotation]:
        if scope is AxiomScope.CLOSURE:
            _reject_document_key(scope, document_key)
            return self._closure_annotations
        key = self._scope_key(scope, document_key)
        return self._annotations_by_key[key]

    def contains(
        self,
        axiom: AxiomNode,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> bool:
        if not isinstance(axiom, AxiomNode):
            raise TypeError("axiom must be an OWL axiom")
        return axiom in self._axioms(scope, document_key)

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
        roots: tuple[StructuralNode, ...] = (
            *self.ontology_annotations(scope=scope, document_key=document_key),
            *self._axioms(scope, document_key),
            *self._extensions(scope, document_key),
        )
        return _signature(roots, kind, include_builtins=include_builtins)

    def view(self, view_type: type[V], /, **options: object) -> V:
        if not isinstance(view_type, type):
            raise TypeError("view_type must be a type")
        if options:
            raise TypeError("OntologySnapshot identity view accepts no options")
        if view_type is OntologySnapshot or isinstance(self, view_type):
            return cast(V, self)
        raise LookupError(f"view type {view_type.__name__} is not available in this build stage")

    def _axioms(self, scope: AxiomScope, document_key: str | None) -> CanonicalSet[AxiomNode]:
        if not isinstance(scope, AxiomScope):
            raise TypeError("scope must be AxiomScope")
        if scope is AxiomScope.CLOSURE:
            _reject_document_key(scope, document_key)
            return self._closure_axioms
        return self._axioms_by_key[self._scope_key(scope, document_key)]

    def _extensions(
        self, scope: AxiomScope, document_key: str | None
    ) -> CanonicalSet[StructuralNode]:
        if not isinstance(scope, AxiomScope):
            raise TypeError("scope must be AxiomScope")
        if scope is AxiomScope.CLOSURE:
            _reject_document_key(scope, document_key)
            return self._closure_extensions
        return self._extensions_by_key[self._scope_key(scope, document_key)]

    def _scope_key(self, scope: AxiomScope, document_key: str | None) -> str:
        if scope is AxiomScope.ROOT:
            _reject_document_key(scope, document_key)
            return self.root_document_key
        if scope is AxiomScope.DOCUMENT:
            if not isinstance(document_key, str) or not document_key:
                raise ValueError("AxiomScope.DOCUMENT requires document_key")
            if document_key not in self._document_by_key:
                raise KeyError(document_key)
            return document_key
        raise AssertionError(scope)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OntologySnapshot):
            return NotImplemented
        return (
            self.structural_fingerprint == other.structural_fingerprint
            and self.import_manifest == other.import_manifest
            and self._closure_axioms == other._closure_axioms
        )

    def __hash__(self) -> int:
        value = int.from_bytes(self.structural_fingerprint.digest[:8], "big", signed=True)
        return -2 if value == -1 else value


@dataclass(frozen=True, slots=True)
class _ScopedDocument:
    annotations: CanonicalSet[Annotation]
    axioms: CanonicalSet[AxiomNode]
    extensions: CanonicalSet[StructuralNode]
    pairs: tuple[tuple[StructuralNode, StructuralNode], ...]
    identity_preserved: bool
    anonymous_scopes: frozenset[bytes]


def _preserved_documents(
    records: tuple[DocumentRecord, ...], documents: tuple[OntologyDocument, ...]
) -> Mapping[str, _ScopedDocument]:
    """Retain already-effective scopes during explicit view materialization."""

    return freeze_mapping(
        {
            record.document_key: _ScopedDocument(
                document.ontology_annotations,
                document.axioms,
                document.extension_components,
                (),
                True,
                _document_anonymous_scopes(document),
            )
            for record, document in zip(records, documents, strict=True)
        }
    )


def _document_anonymous_scopes(document: OntologyDocument) -> frozenset[bytes]:
    return frozenset(
        node.document_scope
        for collection in (
            document.ontology_annotations,
            document.axioms,
            document.extension_components,
        )
        for root in collection
        for node in _walk(root)
        if isinstance(node, AnonymousIndividual)
    )


def _scope_documents(
    records: tuple[DocumentRecord, ...], documents: tuple[OntologyDocument, ...]
) -> Mapping[str, _ScopedDocument]:
    grouped: dict[bytes, list[tuple[DocumentRecord, OntologyDocument]]] = {}
    for record, document in zip(records, documents, strict=True):
        grouped.setdefault(record.document_fingerprint.digest, []).append((record, document))
    result: dict[str, _ScopedDocument] = {}
    for fingerprint in sorted(grouped):
        group = sorted(
            grouped[fingerprint],
            key=lambda item: (item[0].source_sha256, item[0].document_key),
        )
        for ordinal, (record, document) in enumerate(group):
            roots: tuple[StructuralNode, ...] = (
                *document.ontology_annotations,
                *document.axioms,
                *document.extension_components,
            )
            if not any(
                isinstance(node, AnonymousIndividual) for root in roots for node in _walk(root)
            ):
                result[record.document_key] = _ScopedDocument(
                    document.ontology_annotations,
                    document.axioms,
                    document.extension_components,
                    ()
                    if document.origin_index is not None
                    else tuple((root, root) for root in roots),
                    True,
                    frozenset(),
                )
                continue
            scope = hashlib.sha256(
                b"pyowl-core:snapshot-document-scope:v1\x00" + fingerprint + encode_varint(ordinal)
            ).digest()
            replacements: dict[AnonymousIndividual, AnonymousIndividual] = {}
            pairs: list[tuple[StructuralNode, StructuralNode]] = []

            def moved(
                value: StructuralNode,
                selected_scope: bytes = scope,
                selected_replacements: dict[
                    AnonymousIndividual, AnonymousIndividual
                ] = replacements,
                selected_pairs: list[tuple[StructuralNode, StructuralNode]] = pairs,
            ) -> StructuralNode:
                scoped = _rescope_value(value, selected_scope, selected_replacements)
                selected_pairs.append((value, scoped))
                return scoped

            annotations = CanonicalSet(
                cast(Annotation, moved(item)) for item in document.ontology_annotations
            )
            axioms = CanonicalSet(cast(AxiomNode, moved(item)) for item in document.axioms)
            extensions = CanonicalSet(moved(item) for item in document.extension_components)
            result[record.document_key] = _ScopedDocument(
                annotations,
                axioms,
                extensions,
                tuple(pairs),
                False,
                frozenset((scope,)),
            )
    return freeze_mapping(result)


def _rescope_value(
    value: StructuralNode,
    scope: bytes,
    replacements: dict[AnonymousIndividual, AnonymousIndividual],
) -> StructuralNode:
    if isinstance(value, AnonymousIndividual):
        retained = replacements.get(value)
        if retained is None:
            retained, _record = re_scope_anonymous(value, scope)
            replacements[value] = retained
        return retained
    if not _contains_anonymous(value):
        return value
    return cast(StructuralNode, _replace_component(value, scope, replacements))


def _replace_component(
    value: object,
    scope: bytes,
    replacements: dict[AnonymousIndividual, AnonymousIndividual],
) -> object:
    if isinstance(value, AnonymousIndividual):
        retained = replacements.get(value)
        if retained is None:
            retained, _record = re_scope_anonymous(value, scope)
            replacements[value] = retained
        return retained
    if isinstance(value, CanonicalSet):
        return CanonicalSet(
            cast(StructuralNode, _replace_component(item, scope, replacements)) for item in value
        )
    if isinstance(value, tuple):
        return tuple(_replace_component(item, scope, replacements) for item in value)
    if not isinstance(value, StructuralNode) or isinstance(value, (IRI, Entity, Literal)):
        return value
    if not is_dataclass(value):
        return value
    arguments = {
        item.name: _replace_component(getattr(value, item.name), scope, replacements)
        for item in fields(value)
    }
    return type(value)(**arguments)


def _contains_anonymous(value: StructuralNode) -> bool:
    return any(isinstance(item, AnonymousIndividual) for item in _walk(value))


def _walk(value: StructuralNode) -> Iterator[StructuralNode]:
    from pyowl_core.model import walk

    yield from walk(value)


def _merge_origins(
    records: tuple[DocumentRecord, ...],
    documents: tuple[OntologyDocument, ...],
    scoped: Mapping[str, _ScopedDocument],
    *,
    maximum: int,
) -> OriginIndex:
    merged: dict[bytes, list[OriginOccurrence]] = {}
    observed = 0
    for record, document in zip(records, documents, strict=True):
        scoped_document = scoped[record.document_key]
        if scoped_document.identity_preserved and document.origin_index is not None:
            for digest, occurrences in document.origin_index.entries.items():
                for occurrence in occurrences:
                    merged.setdefault(digest, []).append(
                        OriginOccurrence(
                            record.document_key,
                            occurrence.occurrence,
                            occurrence.span,
                        )
                    )
                    observed += 1
                    if observed > maximum:
                        raise ResourceLimitError(
                            "resource limit max_origin_entries exceeded",
                            limit="max_origin_entries",
                            observed=observed,
                            allowed=maximum,
                        )
            continue
        fallback = 0
        for original, moved in scoped_document.pairs:
            original_digest = structural_digest(original)
            moved_digest = original_digest if original is moved else structural_digest(moved)
            occurrences = (
                ()
                if document.origin_index is None
                else document.origin_index.entries.get(original_digest, ())
            )
            if not occurrences:
                occurrences = (OriginOccurrence(record.document_key, fallback),)
                fallback += 1
            for occurrence in occurrences:
                merged.setdefault(moved_digest, []).append(
                    OriginOccurrence(record.document_key, occurrence.occurrence, occurrence.span)
                )
                observed += 1
                if observed > maximum:
                    raise ResourceLimitError(
                        "resource limit max_origin_entries exceeded",
                        limit="max_origin_entries",
                        observed=observed,
                        allowed=maximum,
                    )
    return OriginIndex(
        {digest: tuple(sorted(set(occurrences))) for digest, occurrences in merged.items()}
    )


def materialize_view(
    view: OntologyView,
    *,
    annotations: CanonicalSet[Annotation],
    axioms: CanonicalSet[AxiomNode],
    extensions: CanonicalSet[StructuralNode],
    origin_index: OriginIndex,
    structural_context: StructuralContext,
    structural_fingerprint_override: Fingerprint | None = None,
    limits: object,
    elapsed_seconds: float,
) -> OntologySnapshot:
    """Create a self-contained concrete snapshot from effective view content."""

    from pyowl_core.config import BackendPreference, DocumentFormat, ImportPolicy
    from pyowl_core.limits import ParseLimits

    from .document import OntologyID
    from .imports import DocumentStatus
    from .provenance import DetectionBasis, DigestKind, DocumentProvenance

    if not _is_ontology_view(view):
        raise TypeError("view must implement OntologyView")
    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits")
    if not isinstance(origin_index, OriginIndex):
        raise TypeError("origin_index must be OriginIndex")
    if not isinstance(structural_context, StructuralContext):
        raise TypeError("structural_context must be StructuralContext")
    if structural_fingerprint_override is not None and not isinstance(
        structural_fingerprint_override, Fingerprint
    ):
        raise TypeError("structural_fingerprint_override must be Fingerprint or None")
    limits.enforce("max_axioms", len(axioms))
    limits.enforce("max_annotations", len(annotations))
    limits.enforce("max_origin_entries", sum(len(value) for value in origin_index.entries.values()))
    structural = structural_fingerprint_override or effective_structural_fingerprint(
        structural_context, annotations, axioms, extensions
    )
    source_digest = hashlib.sha256(
        b"pyowl-core:materialized-view-source:v1\x00" + structural.digest
    ).digest()
    provenance = DocumentProvenance(
        source_digest,
        DigestKind.EXACT_BYTES,
        0,
        0,
        None,
        None,
        DocumentFormat.FUNCTIONAL,
        DetectionBasis.EXPLICIT,
        parser="pyowl_core.document.materialize",
        backend="python",
    )
    document = OntologyDocument(
        OntologyID(),
        None,
        (),
        annotations,
        axioms,
        extensions,
        provenance,
        origin_index=origin_index,
    )
    key = (
        "d1:"
        + hashlib.sha256(
            b"pyowl-core:materialized-document-key:v1\x00" + structural.digest
        ).hexdigest()
    )
    record = DocumentRecord(
        key,
        document.ontology_id,
        None,
        source_digest,
        document.document_fingerprint,
        DocumentFormat.FUNCTIONAL,
        DocumentStatus.ROOT,
    )
    manifest = ImportManifest(
        ImportPolicy.IGNORE,
        True,
        hashlib.sha256(b"pyowl-core:materialized-resolver:v1\x00").digest(),
        (record,),
        (),
    )
    options = LoadOptions(
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
        limits=limits,
        offline=True,
    )
    return OntologySnapshot(
        document,
        (document,),
        manifest,
        key,
        options,
        timings={"materialize_seconds": elapsed_seconds},
        _preserve_document_scopes=True,
        _origin_index_override=origin_index,
        _structural_context=structural_context,
        _structural_fingerprint_override=structural_fingerprint_override,
        _complete_override=view.is_complete,
    )


def _signature(
    roots: tuple[StructuralNode, ...],
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


def _reject_document_key(scope: AxiomScope, document_key: str | None) -> None:
    if document_key is not None:
        raise ValueError(f"document_key is not valid for {scope.value} scope")


__all__ = [
    "AxiomScope",
    "CoreCapabilities",
    "LoadReport",
    "OntologyInput",
    "OntologySnapshot",
    "OntologyView",
    "SnapshotProvider",
    "materialize_view",
]
