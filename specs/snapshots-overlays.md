# Immutable snapshots, deltas, overlays, and composition

## 1. Snapshot contents

An `OntologySnapshot` is a frozen resolved view consisting of:

- a root `OntologyDocument`;
- a canonical tuple of closure documents with stable document keys;
- directed import edges and one outcome per import request;
- document-scoped anonymous identity;
- the import policy/resolver configuration fingerprint;
- effective set views over annotated axioms and annotations;
- provenance/origin indexes;
- model/API/wire capability metadata; and
- lazy immutable core indexes.

It is not just a flattened axiom set. Document boundaries are necessary for
anonymous individuals, imports, diagnostics, rendering, and provenance.
Flattened closure iteration is a view.

## 2. Import manifest

`ImportManifest` contains canonical `DocumentRecord` and `ImportEdge` values:

```text
DocumentRecord:
  document_key, ontology_id, document_iri?, source_sha256,
  document_fingerprint, format, status

ImportEdge:
  importing_document_key, import_iri,
  status = resolved | unresolved | ignored | denied | failed,
  resolved_document_key?, resolver_name?, sanitized_locator?, diagnostic?
```

Graph cycles/diamonds are retained. Manifest ordering is canonical. Timing,
machine-local absolute cache path, credentials, and nondeterministic transport
headers are report/provenance fields, not structural fingerprint fields.

`OntologySnapshot.is_complete` means every import edge required by its policy
is resolved. `IGNORE` snapshots are valid but not complete for Direct Semantics;
consumers state their requirement. No boolean named merely `valid` conflates
syntax, closure completeness, OWL 2 DL, or a profile.

## 3. Effective axiom semantics

A closure view is the structural set union of annotated axioms after each
document's anonymous individuals are standardized apart. Equal annotated axioms
from multiple documents appear once in set/iteration results and have multiple
origins. Axioms differing only in axiom annotations are distinct complete
structural axioms; a logical-only view strips annotations explicitly.

Root and per-document views remain available. Consumers must choose scope. A
reasoner normally compiles the logical closure; an editor may operate on root
only; a projector documents whether annotations/imports are included.

## 4. Delta contract

`OntologyDelta` is immutable and canonical. It contains add/remove annotated
axiom sets, add/remove ontology annotations, an optional expected base
structural fingerprint, and nonsemantic string metadata.

Construction rules:

- add/remove intersection is rejected;
- strict removal requires the value in the effective base;
- addition of an existing value is rejected in strict mode;
- expected base mismatch raises `DeltaBaseMismatchError` before work;
- anonymous individuals must have a valid originating/builder document scope;
- metadata is size/key limited and excluded from semantic fingerprints; and
- ontology/import/document identity changes are not axiom deltas.

An explicit `DeltaPolicy.IDEMPOTENT` can turn absent removals/existing additions
into recorded no-ops for replay. It never changes effective fingerprints.

## 5. Overlay contract

`OntologyOverlay` is a sibling implementation of `OntologyView`, not an
`OntologySnapshot` subclass. It references one base `OntologyView` and one
effective delta. Construction does not walk/copy the base axiom set.

Lookup/iteration obeys:

```text
contains(x) = (x in delta.add) or
              (x not in delta.remove and base.contains(x))
```

Canonical iteration merges the base iterator and sorted additions while
filtering removals. Unordered membership uses delta sets plus base indexes.
Overlay fingerprints are computed as content fingerprints, not hashes of edit
history; two edit histories with equal effective views have equal semantic
fingerprints. A separate provenance `edit_chain_digest` may distinguish them.

Core lazy indexes implement delta-aware patching where safe. For example,
axiom-type and entity-reference indexes merge small add/remove postings. A view
whose cost would approach a full rebuild may build and cache a private overlay
index without materializing the base. This work is reported.

### 5.1 Layering and compaction

Applying a delta to an overlay creates another overlay while depth and total
delta size remain within options. Default soft thresholds are depth 32 or delta
entries exceeding 10% of base axioms; thresholds trigger a performance warning
and optional asynchronous recommendation, never hidden semantic work.

`compact()` collapses overlay deltas into the shallowest equivalent persistent
overlay when possible. `materialize()` creates an independent concrete
snapshot. Both are explicit by default, preserve structural/logical/signature
fingerprints, and record time/memory. A consumer may request auto-compaction in
configuration, which is then part of its performance behavior.

## 6. Zero-copy composition

`OntologyComposite` is another sibling `OntologyView`. It retains two or more
member views and an optional bridge delta:

```python
merged = compose_views(
    source,
    target,
    delta=OntologyDelta(add_axioms=bridge_axioms),
    roles=("source", "target"),
)
```

No member is cloned, flattened, serialized, or reparsed. The composite holds
strong references, merges canonical iterators and common index postings, and
keeps each originating document/anonymous scope distinct. Equal named entities
and equal axioms unify structurally; origins retain every member/role.

Member roles are acquisition provenance and excluded from logical identity.
Member order/roles do not change the logical fingerprint. The structural
fingerprint includes a sorted canonical member manifest, member structural
fingerprints, and bridge content but not the caller's argument order. A
composition-provenance digest may retain role/order separately.

Duplicate roles are allowed when meaningful; role count must match member
count. At least two members are required. Direct self-composition and recursive
cycles are rejected. Nested composites are flattened structurally while their
provenance tree remains inspectable.

This is the required coherence/repair primitive for OAEI evaluation. The
reasoner compiles the composite directly. Materialization is explicit and
usually unnecessary.

## 7. Identity-preserving coercion

`coerce_snapshot` accepts any `OntologyView` and returns that exact object after
model/adapter/lifecycle checks:

```python
assert coerce_snapshot(snapshot) is snapshot
assert coerce_snapshot(overlay) is overlay
assert coerce_snapshot(composite) is composite
```

A `SnapshotProvider.owl_snapshot()` may return any `OntologyView`; its returned
identity is preserved. Incompatible model schema or a closed mapped view raises
before a consumer begins. Options that would require different parsing/import
policy are conflicts; coercion never reparses to satisfy them.

## 8. Fingerprints and cache keys

Fingerprints follow `model.md`. Incremental hashing MAY reuse base Merkle-like
summaries, but the result must equal independent canonical full-content hashing.
The optimized and independent implementations are compared in tests.

Required cache key templates:

```text
parser document cache:
  (source_sha256, format, MODEL_SCHEMA_VERSION, parser_options)

structural view cache:
  (structural_fingerprint, view_kind, view_schema, options)

reasoner/projector compiler cache:
  (logical/structural fingerprint, signature fingerprint,
   MODEL_SCHEMA_VERSION, consumer_compiler_schema, semantic options)
```

Acquisition location and mtime are never sufficient cache identity. A fast
stat-based probe may avoid hashing only when a trusted cache ledger also binds
filesystem identity and policy; final reusable identity is content-based.

## 9. Thread/process behavior

Views are safe for concurrent reads. Creating two equal views/indexes may race,
but only one immutable result is published and failures are not cached forever.
Delta construction is local and does not lock a base for its lifetime.

Memory-mapped snapshots own their mapping. Overlays/composites keep their bases
alive. Explicit close of a mapped base with live dependent views either fails
with `SnapshotInUseError` or defers unmapping until dependents release; dangling
pointers are impossible. After fork, mappings can be reopened from the stable
wire file; process-local once locks/native handles are not reused.

## 10. Serialization

`encode_snapshot(view)` writes a self-contained canonical effective snapshot.
It retains origin document/member manifests sufficient for anonymous identity
and provenance, but a decoder need not recreate the original overlay edit tree.
Stable delta/edit-chain wire encoding is a future opt-in section, not required
for v1 IPC.

Programmatic delta JSON MAY exist for human tools only if it uses Functional-
Style structural strings plus explicit schema/fingerprint; it is not a high-
performance or trusted interchange and cannot replace the wire format.

## 11. Acceptance tests

- identity-preserving coercion for all three view classes and providers;
- million-axiom base plus one-axiom overlay shows no base-sized allocation;
- source+target+bridges composition shows no duplicated base arena;
- canonical iteration/membership matches independently materialized sets;
- randomized delta chains and compaction preserve all fingerprints/results;
- duplicate/conflict/idempotent policies produce stable diagnostics;
- anonymous individuals remain separated across imports/composite members;
- concurrent view/index creation, cancellation, close, and fork cases are safe;
- encoder output for equivalent snapshot/overlay/composite content is canonical;
- memory is reclaimed when the last dependent is released; and
- OAEI coherence integration demonstrates zero path reparsing/serialization in
  one process.

