# WP15 — retained native arena and lazy Python facade

## Goal

Replace buffer-returning native helper composition with a safe retained Rust
document/snapshot store behind the existing public Python contracts.

## Read first

`native-ontology-redesign.md`, `architecture.md`, `native-backend.md`,
`model.md`, `contracts.md`, `security.md`, `wire-format.md`, and the WP14
handoff.

## Depends on

WP07, WP09 and WP14.

## Owned paths

Native workspace/registration, builder/model/document/snapshot/facade/lifetime
modules, private stub/backend dispatch and facade storage integration, plus
native arena/lazy-facade tests listed in the manifest. It does not yet advertise
a syntax capability owned by WP16. The V2 publication amendment additionally
owns `schemas/native-snapshot-publication-v2.toml` and its exact deterministic
renderer `tools/schema/native_snapshot_publication_v2.py`; this narrow handoff
exception does not transfer WP17's ownership of other schema tooling.

## Deliverables

- Checked mutable builder and immutable shared arena covering every model tag,
  with interning, ordered/unordered sequences, document-scoped anonymous
  identity, origins, documents, imports, fingerprints, and bounded allocation.
- Private owning `_NativeDocumentHandle`/`_NativeSnapshotHandle` contracts with
  panic, cancellation, GIL, close, mapping, fork, and concurrent-read safety.
- Extension registration and Python backend seams split into stable ingestion
  and view modules. WP16 owns only the ingestion implementation and WP17 owns
  only the view implementation, so those wave-15 packages do not concurrently
  edit `native/src/lib.rs`, `_native.pyi`, `backends/native.py`, or dispatch.
- A versioned builder-to-snapshot handoff (`NativeSnapshotPublicationV1`) frozen
  in a generated ledger and matching Rust/Python protocols. A successful
  canonical freeze returns one owning storage handle plus immutable document/
  import tables, roots, fingerprint inputs/results, bounded diagnostics/report,
  capability bits, and source/provenance manifests. It exposes no parser-
  specific state, Python axiom collection, encoded-view layout, or mutable
  builder reference.
- The `NativeSnapshotPublicationV2` lazy-facade amendment, including exact
  recursive boundary validation, owner-role paging, authoritative content
  manifests, disjoint counters, and a generated/checkable typed TOML ledger.
- Contract fixtures/fakes proving WP16 can assemble an import closure and
  publish the retained snapshot solely through that handoff, while WP17 can
  attach views/index/wire exporters to the same published handle. The snapshot
  facade constructor consumes `NativeSnapshotPublicationV1`; neither successor
  reaches into the other's owned module.
- Public `OntologyDocument`/`OntologySnapshot` facade storage that materializes
  scalar Python values lazily and preserves factory equality/hash behavior.
- Generated/model-fixture construction path and mapped/wire fixture path that
  exercise retained storage before native parsers land.
- Allocation/object/lifetime instrumentation, including bounded or weak facade
  caches and deterministic release when the final owner disappears.

## Acceptance

- Every constructor round-trips between Python storage, retained native storage,
  canonical bytes, and wire with equal values, hashes, fingerprints, order, and
  diagnostics.
- Publishing a million-axiom native fixture creates O(documents + bounded
  metadata) Python objects before scalar access, not O(terms or axioms).
- Full scalar traversal is correct and does not permanently duplicate the
  complete arena through an unbounded facade cache.
- No borrowed buffer/pointer outlives its owner; close/fork/concurrency,
  sanitizers, Miri where applicable, fuzz, panic, cancellation, and allocation-
  failure tests pass.
- No public PyO3 type/private ID appears, and the complete Python backend remains
  independent and passing.
- Empty/stub ingestion and encoded-view registration seams build and fail closed
  without advertising either WP16 or WP17 capability.
- The V1/V2 handoff ledgers, Rust trait/record, Python protocol, generated fakes, and
  closure-publication tests are frozen in the WP15 report. WP16 and WP17 can run
  their complete fixture lanes without editing `facade.rs`,
  `document/snapshot.py`, registration, dispatch, or each other's modules.
- Any later field or lifetime change requires an explicit versioned WP15
  handoff amendment and coordinated fixtures; positional/duck-typed extension
  is forbidden.
