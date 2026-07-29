# Public contracts

This file freezes names and signatures for parallel consumer specifications.
Signatures are illustrative Python 3.10 typing; implementation modules may split
them as described by `architecture.md`, but curated exports remain available.

## 1. Versions

```python
__version__: str                    # package SemVer
API_VERSION: tuple[int, int]        # public contract line
MODEL_SCHEMA_VERSION: int           # canonical identity/fingerprint semantics
WIRE_FORMAT_VERSION: tuple[int, int]
ADAPTER_PROTOCOL_VERSION: int
```

The first implementation line is package `0.1.x`, model schema 1, wire 1.0,
adapter protocol 1. No consumer compares `__version__` lexically; use packaging
version parsing or the explicit tuples.

## 2. Input and configuration values

```python
class DocumentFormat(str, Enum):
    RDF_XML = "rdfxml"
    TURTLE = "turtle"
    OWL_XML = "owlxml"
    FUNCTIONAL = "functional"

class ImportPolicy(str, Enum):
    IGNORE = "ignore"
    RECORD_UNRESOLVED = "record_unresolved"
    RESOLVE_LOCAL = "resolve_local"
    RESOLVE_STRICT = "resolve_strict"

class BackendPreference(str, Enum):
    AUTO = "auto"
    PYTHON = "python"
    NATIVE = "native"

@dataclass(frozen=True, slots=True)
class ParseLimits:
    max_source_bytes: int = 2 * 1024**3
    max_documents: int = 1_000
    max_total_source_bytes: int = 8 * 1024**3
    max_axioms: int = 100_000_000
    max_terms: int = 500_000_000
    max_nesting_depth: int = 512
    max_rdf_list_length: int = 10_000_000
    max_literal_bytes: int = 64 * 1024**2
    max_iri_bytes: int = 1024 * 1024
    max_prefixes: int = 1_000_000
    max_import_depth: int = 128
    max_redirects: int = 5
    max_diagnostics: int = 10_000
    max_memory_bytes: int | None = None
    deadline_seconds: float | None = None

@dataclass(frozen=True, slots=True)
class LoadOptions:
    format: DocumentFormat | None = None
    imports: ImportPolicy = ImportPolicy.RESOLVE_LOCAL
    backend: BackendPreference = BackendPreference.AUTO
    limits: ParseLimits = ParseLimits()
    offline: bool = True
    preserve_source_map: bool = False
    collect_provenance: bool = True
    validate_owl2_dl: bool = False
    deterministic: bool = True
```

Library APIs MUST NOT use a mutable options dictionary. Defaults are secure:
local-only and offline. An application that needs network imports opts in by
providing a resolver configured for approved schemes/hosts.

Input aliases are documented unions, not runtime base classes:

```python
DocumentSource = str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO | TextIO
DocumentInput = DocumentSource | OntologyDocument
OntologyInput = DocumentInput | OntologyView | SnapshotProvider
```

All three aliases are exported from `pyowl_core` as public typing aliases.
Consumers (pyELK, pyHermiT, projector, evaluator) import them rather than
re-declaring equivalent unions.

A plain `str` is a filesystem path, never ontology text and never an arbitrary
URL. Text ontology input uses an explicit text stream or encoded bytes with a
document IRI. A `TextIO` source requires both explicit `format` and
`document_iri`; the exact Unicode code points returned by `read()` are encoded
as UTF-8 for parsing and `source_sha256`, and provenance marks this as a
normalized text-stream digest rather than acquired-byte identity. No Unicode
normalization is applied. URL acquisition belongs to a resolver.

## 3. Loading facade

```python
def parse_document(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO | TextIO,
    *,
    format: DocumentFormat | str | None = None,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
) -> OntologyDocument:
    """Parse exactly one document; record direct imports; never resolve them."""

def load_snapshot(
    source: DocumentInput,
    *,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
) -> OntologySnapshot:
    """Create an immutable closure under the selected import policy."""

@runtime_checkable
class SnapshotProvider(Protocol):
    def owl_snapshot(self) -> OntologyView: ...

def coerce_snapshot(
    source: OntologyInput,
    *,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
) -> OntologyView:
    """Identity-preserving adapter; parse only path/byte/stream inputs."""

def apply_delta(
    base: OntologyView,
    delta: OntologyDelta,
) -> OntologyOverlay: ...

def compose_views(
    *views: OntologyView,
    delta: OntologyDelta | None = None,
    roles: Sequence[str | None] | None = None,
) -> OntologyComposite: ...
```

Rules:

- `coerce_snapshot(x) is x` for a compatible snapshot/overlay.
- For a provider, the exact returned identity is retained after validation.
- `load_snapshot(document)` does not reparse it.
- `document_iri` is root-acquisition metadata, not a reusable load policy. It
  is required for `BinaryIO`/`TextIO`, optional for paths and bytes-like
  sources, and is passed only to the root parser. A path with no explicit value
  retains its absolute `file:` document IRI; bytes with no explicit value
  retain no document IRI. Each imported document obtains its document IRI from
  its `ResolvedDocument`.
- Passing `document_iri` with an already parsed `OntologyDocument`, an existing
  `OntologyView`, or a `SnapshotProvider` raises `OptionConflictError` with
  code `DOCUMENT_IRI_SOURCE_CONFLICT`; the core never rebases or reparses an
  existing object. Invalid argument types raise before stream acquisition.
- `load_snapshot` accepts acquisition/document input only and always returns a
  concrete snapshot; callers with an existing view/provider use
  `coerce_snapshot`, avoiding hidden overlay/composite materialization.
- Incompatible options passed with an existing snapshot raise
  `OptionConflictError`; they never trigger a rebuild silently.
- `parse_document` rejects snapshot/provider input to prevent ambiguous intent.
- Import policy is explicit in the resulting snapshot manifest/fingerprint.
- `coerce_snapshot(composite) is composite`; “snapshot” in the function name
  denotes acquisition of an ontology view and never forces materialization.

## 4. Document and snapshot surface

`OntologySnapshot`, `OntologyOverlay`, and `OntologyComposite` are sibling
concrete implementations of one read-only `OntologyView` protocol. An overlay
or composite is not a snapshot subclass:
its storage/lifecycle and document provenance differ even though consumers can
query both identically. Consumers that can operate on repair overlays MUST type
their primary input as `OntologyView`; APIs that specifically require a fully
materialized resolved closure use `OntologySnapshot`.

```python
@dataclass(frozen=True, slots=True)
class OntologyID:
    ontology_iri: IRI | None = None
    version_iri: IRI | None = None

@dataclass(frozen=True, slots=True)
class CoreCapabilities:
    adapter_protocol: int
    model_schema: int
    wire_format: tuple[int, int]
    features: frozenset[str]
    encoded_view_schemas: Mapping[str, int]
    backend: str

class OntologyDocument:
    @property
    def ontology_id(self) -> OntologyID: ...
    @property
    def document_iri(self) -> IRI | None: ...
    @property
    def direct_imports(self) -> tuple[IRI, ...]: ...
    @property
    def ontology_annotations(self) -> frozenset[Annotation]: ...
    def iter_axioms(self, axiom_type: type[A] | None = None) -> Iterator[Axiom | A]: ...
    def iter_extensions(self, namespace: str | None = None) -> Iterator[ExtensionComponent]: ...
    def signature(self, kind: EntityKind | None = None, *, include_builtins: bool = True) -> tuple[Entity, ...]: ...
    @property
    def document_fingerprint(self) -> Fingerprint: ...
    @property
    def provenance(self) -> DocumentProvenance: ...

@runtime_checkable
class OntologyView(Protocol):
    @property
    def capabilities(self) -> CoreCapabilities: ...
    def iter_axioms(self, axiom_type: type[A] | None = None, *, scope: AxiomScope = AxiomScope.CLOSURE) -> Iterator[Axiom | A]: ...
    def iter_extensions(self, namespace: str | None = None, *, scope: AxiomScope = AxiomScope.CLOSURE) -> Iterator[ExtensionComponent]: ...
    def contains(self, axiom: Axiom, *, scope: AxiomScope = AxiomScope.CLOSURE) -> bool: ...
    def signature(self, kind: EntityKind | None = None, *, scope: AxiomScope = AxiomScope.CLOSURE, include_builtins: bool = True) -> tuple[Entity, ...]: ...
    def view(self, view_type: type[V], /, **options: object) -> V: ...
    @property
    def structural_fingerprint(self) -> Fingerprint: ...
    @property
    def logical_fingerprint(self) -> Fingerprint: ...
    @property
    def signature_fingerprint(self) -> Fingerprint: ...
    @property
    def report(self) -> LoadReport: ...

class OntologySnapshot:
    """Materialized immutable resolved closure implementing OntologyView."""
    @property
    def root(self) -> OntologyDocument: ...
    @property
    def documents(self) -> tuple[OntologyDocument, ...]: ...
    @property
    def import_manifest(self) -> ImportManifest: ...
    # All OntologyView members above.

class EncodedStructuralView:
    """Versioned read-only bulk structural columns owned by an OntologyView."""
    @property
    def schema_name(self) -> str: ...
    @property
    def schema_version(self) -> int: ...
    @property
    def model_schema(self) -> int: ...
    @property
    def owner(self) -> OntologyView: ...
    @property
    def buffers(self) -> Mapping[str, memoryview]: ...
    @property
    def descriptor(self) -> bytes: ...
    @property
    def structural_fingerprint(self) -> Fingerprint: ...
```

`view.view(EncodedStructuralView, schema_version=1, scope=...)` is the stable
bulk acquisition shape frozen by WP17. Every returned memoryview is read-only
and remains valid while the encoded view/owner is alive. Exact buffer names,
rows, tags, and segment rules live in the generated schema ledger required by
`indexes-views.md`; they do not expose private native layout.

Built-in direct, overlay, composite, decoded, and mapped views advertise
`ontology-identity-index` and support `view(OntologyIdentityIndex)` without
layout introspection. Successfully decoded and mmap-validated wire sources also
advertise `wire-v1` and `wire-verified`; direct parsed/loaded snapshots do not.

“Materialized” in `OntologySnapshot` means that the resolved closure/edit view
has been frozen into one concrete semantic snapshot. It does not require eager
creation of Python objects for every native/mapped term or axiom.

`iter_axioms` canonical order is guaranteed only when `order="canonical"` is
requested by the concrete overload; default iteration is stable for a snapshot
but callers must not treat it as semantic. Filter selection uses exact
constructor unless an explicit family/category filter is supplied.

`AxiomScope.ROOT` returns root-document axioms; `CLOSURE` returns the structural
set union after document-scoped anonymous identity; `DOCUMENT` requires a
document key. Duplicate structurally equal axioms are collapsed in set views,
while provenance records every origin occurrence.

## 5. Delta and overlay

```python
@dataclass(frozen=True, slots=True)
class OntologyDelta:
    add_axioms: frozenset[Axiom] = frozenset()
    remove_axioms: frozenset[Axiom] = frozenset()
    add_ontology_annotations: frozenset[Annotation] = frozenset()
    remove_ontology_annotations: frozenset[Annotation] = frozenset()
    expected_base_fingerprint: Fingerprint | None = None
    metadata: Mapping[str, str] = immutable_mapping()

class OntologyOverlay:
    """Persistent read-through view implementing OntologyView; not a subtype of Snapshot."""
    @property
    def base(self) -> OntologyView: ...
    @property
    def delta(self) -> OntologyDelta: ...
    @property
    def depth(self) -> int: ...
    def materialize(self) -> OntologySnapshot: ...
    def compact(self) -> OntologyOverlay | OntologySnapshot: ...

@dataclass(frozen=True, slots=True)
class CompositeMember:
    view: OntologyView
    role: str | None = None

class OntologyComposite:
    """Zero-copy union over two or more views, optionally plus bridge axioms."""
    @property
    def members(self) -> tuple[CompositeMember, ...]: ...
    @property
    def delta(self) -> OntologyDelta: ...
    def materialize(self) -> OntologySnapshot: ...
```

A delta cannot add and remove the same canonical axiom. Removal of an absent
axiom is an error in strict mode and a recorded no-op only under an explicit
lenient construction option. Import changes are represented by constructing a
new document/snapshot, not an axiom delta, because they change closure identity
and resolver provenance.

Composition retains strong references to each base view and merges iterators
and core indexes without copying their arenas. Roles such as `"source"` and
`"target"` are provenance only; logical fingerprints are independent of member
order/roles, while the structural/composition fingerprint includes a canonical
member manifest. Duplicate axioms collapse structurally and retain all origins.
An anonymous individual remains scoped to its originating document/member.
This is the required OAEI coherence path: compose two loaded views and a bridge
delta, then give the composite directly to a reasoner compiler.

## 6. Fingerprints

```python
@dataclass(frozen=True, slots=True, order=True)
class Fingerprint:
    algorithm: Literal["sha256"]
    schema: int
    digest: bytes                 # exactly 32 bytes

    @property
    def hex(self) -> str: ...
```

No `__hash__` value is persisted: Python hashes are process-width/key specific.
Fingerprints are SHA-256 over domain-separated canonical encodings. The domains
and exact inclusions are frozen in `model.md` and `snapshots-overlays.md`.

## 7. Wire facade

```python
def encode_snapshot(snapshot: OntologyView) -> bytes: ...

def decode_snapshot(
    data: bytes | bytearray | memoryview | BinaryIO,
    *,
    limits: ParseLimits | None = None,
    verify: bool = True,
) -> OntologySnapshot: ...

def open_snapshot(
    path: str | os.PathLike[str],
    *,
    mmap: bool = True,
    limits: ParseLimits | None = None,
    verify: bool = True,
) -> OntologySnapshot: ...

def write_snapshot(
    snapshot: OntologyView,
    path: str | os.PathLike[str],
    *,
    atomic: bool = True,
) -> Fingerprint: ...
```

Encoding an overlay produces a self-contained canonical snapshot by default;
an experimental delta-wire form is not stable IPC until separately specified.
`verify=False` may skip full content hashing only for a caller-authenticated
local cache; structural bounds/reference checks remain mandatory.

## 8. Import resolver

```python
@dataclass(frozen=True, slots=True)
class ImportRequest:
    import_iri: IRI
    importing_document_iri: IRI | None
    chain: tuple[IRI, ...]
    limits: ParseLimits

@dataclass(frozen=True, slots=True)
class ResolvedDocument:
    source: bytes | BinaryIO | os.PathLike[str]
    document_iri: IRI
    format: DocumentFormat | None = None
    expected_sha256: bytes | None = None
    provenance: Mapping[str, str] = immutable_mapping()

@runtime_checkable
class ImportResolver(Protocol):
    def resolve(self, request: ImportRequest) -> ResolvedDocument | None: ...
```

The core supplies `MappingResolver`, `CatalogResolver`, `DirectoryResolver`,
`CompositeResolver`, and an opt-in `HttpResolver`. Resolver results are checked
against the request policy, scheme/host/path allowlists, redirects, byte limits,
and optional digest before parsing.

## 9. Index/view surface

Stable built-in view types are:

- `SignatureView`
- `AxiomTypeIndex`
- `EntityReferenceIndex`
- `DeclarationIndex`
- `AnnotationAssertionIndex`
- `AssertedClassHierarchyView`
- `AssertedPropertyHierarchyView`
- `PropertyDomainRangeView`
- `ExpressionOccurrenceIndex`

Views expose immutable sequences/sets or bounded iterators. They are structural:
“asserted hierarchy” never includes inferred transitive edges. Full contracts
are in `indexes-views.md`.

## 10. Diagnostics and errors

```python
class Severity(str, Enum): INFO = "info"; WARNING = "warning"; ERROR = "error"

@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    document_iri: IRI | None = None
    source_span: SourceSpan | None = None
    import_chain: tuple[IRI, ...] = ()
    details: Mapping[str, str | int | bool] = immutable_mapping()
```

Stable public exception roots:

```text
PyOWLCoreError
├── ModelError
│   ├── InvalidIRIError
│   ├── InvalidLiteralError
│   └── StructuralConstraintError
├── ParseError
│   ├── FormatDetectionError
│   ├── OntologySyntaxError
│   └── UnsupportedSyntaxError
├── ImportResolutionError
│   ├── UnresolvedImportError
│   ├── ImportCycleError
│   ├── DocumentIdentityConflictError
│   ├── IntegrityError
│   └── AccessDeniedError
├── ProfileError
├── ResourceLimitError
├── OperationCancelledError
├── ReentrancyError
├── BackendError
│   ├── BackendUnavailableError
│   └── BackendProtocolError
├── WireError
│   ├── WireVersionError
│   ├── WireCorruptionError
│   └── WireLimitError
├── DeltaError
│   └── DeltaBaseMismatchError
├── OptionConflictError
├── SnapshotLifecycleError
│   ├── ClosedSnapshotError
│   └── SnapshotInUseError
└── AdapterError
    └── AdapterCompatibilityError
```

Exceptions have stable `.code`, optional `.diagnostic`, and chained causes.
Messages are for people, never program control flow. `MemoryError`,
`KeyboardInterrupt`, and `SystemExit` are not wrapped. Native panics cannot cross
FFI and map to `BackendProtocolError(code="NATIVE_PANIC")`.

No public exception name shadows a Python built-in or a well-known stdlib
class: the parse failure is `OntologySyntaxError`, never built-in
`SyntaxError`, and cooperative cancellation raises `OperationCancelledError`,
which is unrelated to `asyncio.CancelledError` and inherits from
`PyOWLCoreError`.

Warning categories include `NativeBackendUnavailableWarning`,
`UnresolvedImportWarning`, `FormatGuessWarning`, `LossyRenderWarning`, and
`DeprecatedAPIWarning`. Fallback warning behavior is fixed in `SPEC.md`.

An ordinary ontology import cycle is legal and never raises
`ImportCycleError`: closure traversal visits each canonical document once.
`ImportCycleError` is reserved for a resolver/catalog alias or HTTP redirect
cycle, or a cycle that violates an explicit resolution resource policy.

## 11. Typing and extension rules

The package ships `py.typed`, complete `.pyi` for the private extension, and
strict type tests on all public examples. Public union aliases are usable on
3.10 without importing `typing_extensions` at runtime unless a feature truly
requires it.

Only documented protocols are structural extension points. Private attributes,
private arena IDs/buffers, and lazy-cache internals are never consumed. Dense
IDs and read-only buffers explicitly published by `EncodedStructuralView` are
the sole bulk exception and are valid only under that view's owner/schema rules.
Third-party parser/writer plugins are explicit by name; merely installing one
must not execute it or alter `auto` behavior.
