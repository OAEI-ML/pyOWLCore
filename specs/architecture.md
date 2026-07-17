# Architecture, ownership, and lifecycle

## 1. Layering

```text
pyowl_core.model          immutable OWL structural values (stdlib only)
        ^
pyowl_core.document       document/snapshot/delta/overlay contracts
        ^
pyowl_core.index          lazy structural indexes and views
        ^
pyowl_core.io             parsing, writing, import resolver, provenance
        ^
pyowl_core.wire           validated canonical persistence/IPC
        ^
pyowl_core.api            load/coerce/encode facade

pyowl_core.backends.python  complete reference implementation
pyowl_core.backends.native  private adapter to pyowl_core._native
```

The arrows mean “may depend on.” The model never imports a parser, resolver,
index, backend, RDF implementation, or consumer. The Python backend cannot call
the native backend. The native extension cannot import a consumer.

Suggested repository tree:

```text
src/pyowl_core/
  __init__.py              curated public exports only
  api.py                   parse/load/coerce/wire facade
  model/                   primitives, expressions, axioms, rules
  document/                document, snapshot, delta, overlay, fingerprints
  index/                   signatures, axiom/annotation/reference views
  io/                      source, formats, parser/writer, resolver, catalogs
  wire/                    schema, encoder, decoder, mmap
  adapters/                provider protocol and explicit plugin registry
  backends/                dispatch, python, native
  diagnostics.py
  exceptions.py
  limits.py
  _native.pyi              private extension typing
native/                    Rust cdylib and parser/model/wire kernels
```

## 2. Ownership boundary

The core owns syntax-neutral asserted structure. It may expose cheap structural
indexes such as “axioms referencing IRI X” or “asserted `SubClassOf` axioms.” It
does not expose entailments or a reasoner's normalized substitutes for axioms.

Examples of consumer-private values:

- pyELK: polarity/indexed class-expression IDs, context premises/conclusions,
  saturation rules, taxonomy nodes;
- pyHermiT: normalized axioms, role automata, DL clauses, predicates, tableau
  nodes, dependencies, blocking labels, classification caches;
- OWL2Vec*: projection-rule plan, encoded adjacency/edge arrays;
- Exact-OM: label bundles, matching exclusions, candidate features; and
- evaluator: repair batches, metric-specific reasoner sessions.

Consumers cache private IR using `(core model schema, logical or structural
fingerprint, consumer compiler schema, options)`. Core must not know or purge
consumer caches.

## 3. Representation strategy

The public values behave as frozen Python value objects regardless of backend.
Implementations may use:

- interned immutable Python objects in the fallback;
- a validated canonical read-only buffer plus lightweight Python handles;
- snapshot-local dense IDs behind accessors; or
- copy-on-write/persistent maps for overlays.

Snapshot-local IDs are not equality, not serialized API identifiers, and not
valid across compaction or decoding. Public equality and hashes use canonical
structure. No caller receives a mutable buffer or pointer.

Native-backed and Python-backed values must interoperate. Equality cannot
perform a per-node FFI call in a hot loop; bulk iterators and encoded column
views are permitted through private/experimental APIs only after lifetime and
fallback parity are specified.

## 4. Object lifecycle

### 4.1 Document

`parse_document` consumes one source and returns one immutable
`OntologyDocument`. It records ontology identity, direct import declarations,
annotations, axioms, prefix/source metadata, parser diagnostics, and an
optional source map. It performs no network or recursive import work.

### 4.2 Snapshot

`load_snapshot` obtains or accepts the root document, resolves imports under an
explicit policy, standardizes anonymous-individual scope per document, and
freezes a closure plus resolution manifest. The snapshot is safe for concurrent
read-only access.

### 4.3 Overlay

`apply_delta` creates a persistent `OntologyOverlay` that references its base
and immutable add/remove sets. Iterators merge without copying the base.
Repeated layers may be compacted only explicitly or at a documented threshold;
compaction preserves structural/logical fingerprints and is observable in
metrics. See `snapshots-overlays.md`.

### 4.4 Close and mapping

Ordinary in-memory snapshots need no `close`. A memory-mapped snapshot is a
context manager and owns a mapping handle. Values obtained from it are either
independent immutable values or keep the mapping alive safely. Access after an
explicit close raises `ClosedSnapshotError`; it never reads dangling memory.

## 5. In-process communication

`coerce_snapshot(snapshot) is snapshot` is a release-blocking invariant. A
consumer must not serialize, clone, or rebuild it. `SnapshotProvider` lets a
wrapper such as Exact-OM publish the same identity:

```python
@runtime_checkable
class SnapshotProvider(Protocol):
    def owl_snapshot(self) -> OntologyView: ...
```

`coerce_snapshot(provider)` invokes the method once, checks the adapter protocol
and model schema, and returns the supplied object. Provider methods must be
idempotent and must not parse on each call.

`OntologySnapshot`, `OntologyOverlay`, and zero-copy `OntologyComposite` are
sibling implementations of the read-only `OntologyView` protocol. Consumers accepting transient repairs type
their input as `OntologyView`. `coerce_snapshot(overlay) is overlay`, just as it
preserves snapshot identity; the function name describes acquisition, not a
forced materialization. Only `load_snapshot` and `overlay.materialize()` promise
a concrete `OntologySnapshot`.

`compose_views(source, target, delta=bridges, roles=("source", "target"))`
creates a composite retaining both base arenas and merging only iterators/lazy
indexes. It is the OAEI coherence/repair primitive; it does not synthesize a
fake root ontology or reparse either side. `coerce_snapshot(composite) is
composite`. Materialization is explicit.

Shared lazy indexes are cached on the snapshot so pyELK, Exact-OM, and the
projector asking for the same core `AxiomTypeIndex` reuse it. Private consumer
indexes are never placed there merely to share memory.

## 6. Cross-process communication

The only stable cross-process object interchange is the wire format:

```text
producer snapshot -> encode_snapshot/open cache -> immutable bytes/file
                                           |
consumer -> decode_snapshot or open_snapshot(mmap=True) -> equivalent snapshot
```

Paths to original ontology documents are acquisition references, not IPC.
Pickle, `marshal`, JSON dumps of Python object internals, and native pointer
sharing are forbidden. Shared-memory transport MAY carry the same validated
wire bytes with an authenticated length/fingerprint envelope.

## 7. Concurrency and reentrancy

- Public structures are immutable and safe for concurrent readers.
- Lazy index creation uses per-index once cells/locks; unrelated indexes can
  build concurrently, and exceptions do not leave partially published state.
- Long native parse/index/wire operations release the GIL only while holding no
  borrowed Python memory and calling no Python callback.
- Parser/resolver callback invocation occurs with the GIL and outside internal
  locks; recursive calls either work with independent state or raise a stable
  `ReentrancyError` before mutation.
- Cancellation and deadlines are polled at bounded intervals. Cancellation
  publishes no partial document/snapshot/cache file.
- Forked children cannot reuse active native parser/index sessions or mappings
  with process-local locks; immutable plain bytes remain usable.

## 8. Backend dispatch

Backend selection happens once per top-level operation and is recorded in
diagnostics. Mixed backend operation is permitted only through stable model or
wire values; it must not perform a hidden full round trip. A native capability
matrix is checked before work begins. `auto` falls back before parsing, not
halfway through a document.

Parser backends return the same canonical model and stable diagnostics. A
backend-specific source span may differ only where the source format does not
define an exact location; error category, rule ID, and offending construct must
remain equivalent.

## 9. Observability

Every top-level load exposes a `LoadReport` with:

- backend and versions;
- detected/forced syntax;
- bytes and axioms per document;
- import resolution attempts/outcomes and cache hits;
- parse, canonicalization, resolution, index, and wire timings;
- peak tracked memory and limit headroom;
- warnings/diagnostics; and
- all fingerprints.

Reports contain sanitized IRIs/paths under the caller's redaction policy and no
source credentials. Metrics are optional and bounded; disabling them does not
change results.

## 10. Import rules for consumers

Consumers import from curated public modules or `pyowl_core`, never
`pyowl_core._native`, `.backends`, internal arena modules, or parser-specific
adapters. Static import tests enforce this across the workspace. No reverse
dependency from core to Exact-OM, pyELK, pyHermiT, projector, or evaluator is
allowed; integration test fixtures may depend on them only in a separate test
environment.
