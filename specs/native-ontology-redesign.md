# Native ontology engine redesign and performance program

Status: normative successor plan for the post-WP13 implementation. It changes
the optimized implementation shape, not OWL semantics or the complete
pure-Python fallback. Where an older work-package brief describes native code
as a collection of buffer-returning accelerators, this specification requires
the retained-native design below.

## 1. Outcome and success hierarchy

The optimized package MUST behave as a native Rust ontology engine behind the
existing Python contracts, rather than as a Python ontology graph that invokes
isolated Rust helpers. A successful native load retains one compact ontology
representation and lets Python and every workspace consumer reuse it without
reparsing, serializing, or eagerly recreating all structural values as Python
objects.

Performance objectives are ordered deliberately:

1. preserve complete OWL 2, import, canonical identity, diagnostic, security,
   determinism, and Python/native parity contracts;
2. remove ontology-sized Python materialization and repeated conversion from
   the normal native path;
3. attempt to outperform every pinned comparator, including Horned-OWL and
   OWLAPI, for equivalent large-ontology loading;
4. at minimum, demonstrate statistically equivalent query-ready native loading
   performance to Horned-OWL under the gates in `performance.md`; and
5. outperform reparsing approaches at workflow level by reusing the same
   retained snapshot across Exact-OM, pyELK, pyHermiT, projection, and OAEI.

Horned equivalence is the minimum performance outcome, not permission to copy
Horned behavior, omit required work, or weaken a correctness gate. Until the
comparative gate passes on approved evidence, documentation MUST describe the
native backend as experimental acceleration and MUST NOT claim Horned/OWLAPI
parity.

## 2. Definitions and fair boundaries

`Native ontology engine` means Rust owns parsing, structural mapping,
interning, canonicalization, immutable storage, fingerprints, common indexes,
and wire/mmap access for every capability advertised as native.

`Retained-native document` and `retained-native snapshot` mean the public
`OntologyDocument` or `OntologySnapshot` is a Python facade that owns a private
native storage handle. The complete ontology remains in immutable native
tables. Python structural objects are created only when a caller requests the
scalar API and MAY then be cached.

`Query-ready single-document load` begins with ontology bytes already resident
in memory and ends only when the result can enumerate axioms, answer a complete
signature query, report imports/ontology identity, and expose all diagnostics
required by the selected options. It includes syntax parsing, RDF-to-OWL
mapping where applicable, validation, interning, deduplication,
canonicalization, and construction of the indexes needed for those readiness
queries. It excludes network acquisition and recursive import fetching, which
are separately measured.

`Query-ready closure load` additionally includes parsing every resolved
document, document-scoped anonymous identity, closure assembly, provenance,
required fingerprints, and publication of one immutable snapshot. Import
resolver I/O is measured separately from core CPU work.

A comparator is equivalent only when it uses the same immutable input bytes,
syntax, import policy/closure, annotation and rule retention, validation level,
source-map choice, and result readiness. Raw tokenization cannot be compared
with a complete pyowl-core snapshot and presented as ontology-loading parity.

Horned comparison has two deliberately different lanes. `horned-model-ready`
ends when Horned's own model is available and is phase-diagnostic only; it
cannot support a query-ready-equivalence claim. `common-contract-ready` adds a
versioned independent adapter that emits the same canonical structural ledger,
four core fingerprint preimages/digests, ontology/document/import identity,
diagnostic inventory, and bounded provenance required from pyowl-core. Every
adapter traversal, canonicalization, and digest on the Horned side is inside
that lane's timer and reported separately from Horned engine time. Pyowl-core's
corresponding canonicalization, freeze, fingerprints, and provenance remain
inside its timer. Only post-timer byte/count equality assertions are excluded.
A Horned model that cannot supply an input needed by the common adapter makes
that corpus ineligible; the benchmark never invents or silently drops it.

The `<= 1.10` aggregate gate therefore means full pyowl-core readiness versus
Horned plus the measured work needed to reach the same common contract, not
full pyowl-core versus raw Horned parsing. Raw Horned remains useful context and
is reported as the stronger, explicitly asymmetric “pyowl-core including
canonical identity/provenance” comparison. WP14 may propose a threshold change
only after the pin ledger and representative raw evidence exist; such a change
is a reviewed contract amendment, never a relabeling of the raw lane.

## 3. Target architecture

```text
path / bytes / stream
          |
  Python acquisition and import policy
          |
  Rust streaming syntax parser
          |
  Rust RDF-to-OWL / structural mapper
          |
  mutable, bounded NativeOntologyBuilder
          |
  canonical freeze exactly once
          |
 Arc<NativeOntologyStorage> / read-only mapped storage
       |             |                  |
 lazy Python     EncodedStructuralView  canonical wire/cache
 facade          (read-only buffers)    and mmap reopen
       |             |
 scalar users    pyELK / pyHermiT / projector / Exact-OM / OAEI
```

The pure-Python backend implements the same public behavior using Python
storage. Backend choice is an implementation detail reported through
capabilities and diagnostics; it does not create a second semantic model.

## 4. Native storage contract

The native engine MUST use dense, typed, immutable tables with snapshot-local
IDs for at least strings, IRIs, entities, literals, anonymous individuals,
annotations, sequences, class/data/property expressions, axioms, documents,
origins, and import records. Implementations SHOULD prefer structure-of-arrays
or another measured scan-efficient layout. Repeated IRIs, strings, literals,
and structural expressions MUST be interned once per retained storage graph.

Construction uses a mutable builder that:

- validates resource limits before allocation growth;
- uses checked identifiers, offsets, counts, and arithmetic;
- does not expose partially built state;
- preserves ordered sequence semantics and canonicalizes unordered operands;
- applies document scope to anonymous individuals before closure union;
- records provenance without copying structural axioms per origin; and
- freezes once into immutable shared ownership.

Freeze MUST NOT perform a native-to-Python ontology conversion. It produces
deterministic tables, postings, fingerprints, and capability metadata. Hash
maps used while building never determine observable order. Native storage may
use implementation-local IDs, but public equality, hashes, fingerprints, and
wire bytes remain defined solely by the core model schema.

Documents in one import closure SHOULD share intern pools when doing so does
not merge anonymous scope or provenance. A snapshot, overlay, or composite
retains strong references to base arenas and adds only bounded manifests,
postings, and deltas. Closure construction MUST NOT duplicate every imported
axiom into a second native arena merely to provide flattened iteration.

## 5. Lazy Python facade

The public classes and signatures in `contracts.md` remain authoritative. A
native-backed public document/snapshot owns a private extension handle, while
public entities, expressions, and axioms are materialized lazily on scalar
access. Materialized values MUST have the same types and semantics as values
created by the Python model factory.

The following are release-blocking native-path requirements:

- parsing/freezing a document creates only O(documents + diagnostics + fixed
  facade metadata) Python objects before scalar access, not O(axioms or terms);
- `iter_axioms()` may materialize values as requested, but bulk consumers are
  not forced through that iterator;
- native-backed and Python-backed values compare and hash identically;
- handle caches are bounded or weak where retaining every visited value would
  recreate the complete Python heap;
- closing/mapping/fork behavior cannot leave a dangling native reference; and
- a consumer receiving an existing view preserves its exact public identity.

An operation whose required native capability is incomplete selects the Python
backend before consuming input under `AUTO`, or raises under forced `NATIVE`.
There is no partially native parse followed by an undocumented Python rebuild.

### 5.1 Retained publication and facade boundary

WP15 freezes `NativeSnapshotPublicationV2` as the typed, digest-bound handoff
between retained storage and Python. Publication construction recursively
revalidates exact record, enum, scalar, tuple, byte, and nested metadata types,
plus their semantic invariants, before it calls an owner. Reading the envelope
may inspect metadata, the O(documents) cardinality binding, attestations, and
counters only. It MUST NOT request a facade page, decode a structural or
auxiliary row, recompute a content manifest, or traverse retained ontology
tables. The generated owner binding is O(documents) plus fixed work.

The owner roles are intentionally distinct:

- a document handle exposes raw document structural and origin rows;
- a snapshot handle at document scope exposes effective document structural
  and origin rows;
- a snapshot handle at closure scope exposes the effective merged and
  deduplicated structural/origin indexes; and
- raw source metadata and document-scoped RDF mapping reports keep their
  collection-defined scope regardless of which eligible owner serves them.

Raw `SOURCE_MAP_ENTRIES` and raw document `ORIGIN_ENTRIES` are grouped by
ascending structural digest. Within one digest group they preserve exact
producer order and multiplicity, including identical encoded rows. This rule
is enforced across page boundaries as well as within a page. Effective
document and closure origins instead are strictly ascending and unique by
`(digest, document-key UTF-8, occurrence, encoded row)`. Structural rows,
signature projections, RDF rule IDs, and OWL role indexes remain canonical
ascending unique. Source prefixes are unique by the prefix key alone, not by a
prefix-plus-IRI pair. RDF unconsumed triples, RDF diagnostics, and both OWL
issue sequences preserve producer order and multiplicity.

Digest-filtered source/origin paging uses two prefix-bound binary searches over
the retained digest groups, then copies only the requested page-sized window.
Its cursor and total are relative to that digest group; it MUST NOT slice or
materialize the complete group. Page-size choices cannot change row order,
multiplicity, or manifest bytes.

Every page and contains request is reconstructed as an exact V2 request before
the owner call. Returned rows are decoded and canonicalized once at the facade
boundary under the exact `ParseLimits` retained in the publication. A contains
payload is likewise authoritatively decoded once at that boundary; its private
validated axiom is passed to the owner without a second decode. OWL role-edge
pages retain both private validated endpoints so later access does not decode
them again. Capability, coordinate, cardinality, row-size, and configured-limit
checks occur before an ineligible owner call.

Content-manifest validation routes source and raw/effective document origins
only to the named document, routes closure origins by each embedded document
key, and builds each document structural-digest index once. Fingerprint
evidence is accepted only when its length, SHA-256, schema, tag, and published
V1 fingerprint agree with the authoritative preimage. Those preimages are
validation inputs only: an owner MUST discard them before retention and MUST
exclude their size from retained-metadata counters. Retained counters count
disjoint raw, effective, and closure storage exactly once and keep native row
emission separate from Python materialization/decoding.

## 6. Streaming parsing and structural mapping

The primary optimization order is RDF/XML, then Turtle, OWL/XML, and Functional
Syntax unless pinned corpus profiles justify a reviewed change. The native
RDF/XML and Turtle paths MUST stream from bounded input buffers into the
structural builder without first constructing an RDFLib graph or a Python
triple collection proportional to the ontology.

RDF syntaxes require a complete native implementation of the normative OWL 2
RDF mapping, including lists, blank nodes, annotations, negative assertions,
property chains, datatype constructs, ontology headers, imports, malformed and
ambiguous graphs, and stable diagnostics. Parser success without complete
mapping is not an advertised native format capability.

Python retains acquisition policy and resolver callbacks. It supplies owned
bytes or bounded stream chunks and receives a retained-native document plus a
bounded report. Rust code calls no Python callback while the GIL is released.
Import scheduling may parse independent documents concurrently with an
explicit worker limit, but closure order and all results remain deterministic.

## 7. Zero-copy consumer access

Scalar Python iteration is the compatibility path. Performance-sensitive
consumers MUST be able to request the versioned `EncodedStructuralView` defined
in `indexes-views.md` through `OntologyView.view(...)`. Its documented
little-endian tables, dictionaries, descriptors, fingerprints, and owner
provide a stable bulk structural boundary without exposing a raw pointer,
PyO3 class, Rust enum, or mutable memory.

The native backend SHOULD expose its frozen columns directly when they already
match the encoded-view schema. The Python fallback MUST be able to construct
semantically identical buffers, although it may copy. Native consumer
extensions retain the encoded view owner for the complete borrow lifetime and
compile their private IR in coarse calls. They MUST NOT:

- call back into Python once per axiom or term;
- serialize to wire and decode merely to communicate in the same process;
- import `pyowl_core._native` or depend on native arena layout; or
- treat dense IDs as semantic or persist them beyond the encoded-view owner.

Overlay and composite encoded views use base buffer references plus delta/
posting segments where possible. A repair trial over two resident ontologies
must not flatten either base before pyELK or pyHermiT compilation.

## 8. Canonical wire cache and mmap

The stable PYOCORE wire format remains the only cross-process representation.
Native frozen tables SHOULD align with wire and encoded-view columns where that
reduces transforms, but native object layout never becomes the wire contract.

`write_snapshot` produces a content-addressed canonical artifact keyed by
source/closure content, model and wire schema, parser options, and resolver
manifest. `open_snapshot(mmap=True)` MUST publish a lazy native-capable view
after bulk validation without per-row Python construction or an eager complete
native copy. Repeated Exact-OM/OAEI runs SHOULD therefore pay validation and
page-fault costs rather than syntax parsing and structural reconstruction.

Machine-dependent dumps, unchecked native serialization, pickle, and cached
raw pointers remain forbidden.

## 9. Horned-OWL and OWLAPI policy

Horned-OWL is a required development comparator, not a semantic authority.
The comparative harness pins its exact source/crate version and features.
Published documentation and black-box behavior may inform architectural
evaluation, but source reuse or linkage requires the legal and artifact review
in `SPEC.md` and `packaging.md`. The default design remains independently owned
native storage.

An optional Horned loader MAY be investigated as an explicitly installed
adapter. It cannot become an undeclared dependency, escape Horned public
objects, or route each axiom through Python. Its results must pass the same
model/conformance suite, and its parse plus conversion/freeze time is included
in every pyowl-core performance claim.

OWLAPI is a development-only comparator executed in an isolated Java
environment. No Java file, dependency, command, or detection path enters a
distributed pyowl-core artifact or normal test environment.

## 10. Successor work plan

The completed WP00-WP13 reports remain evidence for the first implementation.
The redesign proceeds through these successor packages:

1. **WP14 — contract and comparator baseline:** freeze timed boundaries,
   encoded-view schema direction, retained-storage invariants, pinned corpora,
   and comparable Horned/OWLAPI/current-backend evidence.
2. **WP15 — retained native arena and lazy facade:** implement native builders,
   immutable storage/lifetimes, lazy scalar materialization, and direct/mapped
   storage parity using generated structural inputs; freeze disjoint ingestion
   and view binding/adapter seams for the parallel packages.
3. **WP16 — native streaming ingestion:** implement complete format parsing,
   RDF mapping, native canonical freeze, import-document assembly, and
   differential/security tests, starting with RDF/XML.
4. **WP17 — native views, indexes, wire and consumer handoff:** stabilize
   `EncodedStructuralView`, direct native columns, mmap/cache, delta/composite
   postings, and zero-copy compilation fixtures. WP16 and WP17 may run in
   parallel after WP15 freezes the storage handoff; WP18 owns their complete
   format × encoded-view integration cross-product.
5. **WP18 — integration, optimization and comparative release gate:** profile
   whole pipelines, remove remaining copies and boundary calls, run consumer
   parity, build/audit wheels, and meet the performance outcomes below.

The dependency and ownership details are normative in
`workpackages/manifest.toml` and the individual WP14-WP18 briefs.
Once WP17 freezes a candidate encoded schema, coordinated companion work in
pyELK, pyHermiT, the projector, Exact-OM, and OAEI adopts that public capability
while retaining scalar fallback. Those cross-repository edits remain owned by
their repositories/specifications and may proceed in parallel.

The companion packages are named explicitly so implementation agents do not reopen completed
consumer milestones:

- pyELK WP14, `specs/native-structural-ingestion.md` and
  `WP14-native-structural-compiler.md`;
- pyHermiT WP18, `specs/native-structural-ingestion.md` and
  `WP18-native-structural-compiler.md`;
- projector P7, `specs/native-structural-ingestion.md` and
  `WP-7-encoded-native-compiler.md`;
- Exact-OM WP-N, `WP-N-native-view-handoff.md`; and
- OAEI 0.2.x, `0.2.x-encoded-reasoner-compatibility.md`.

Core WP17 freezes and publishes the public schema/fixtures. The three native consumers own their
private compilers and may work concurrently. Exact and OAEI perform only capability/range,
provenance, identity/counter, fallback, and end-to-end compatibility changes; they do not decode
the buffers or acquire private consumer dependencies.

WP18 records two independent decisions. `core_release_eligible` covers the core-owned semantic,
security, retained-storage, encoded-view, packaging, and Horned common-contract gates. It uses the
independent encoded-view decoder plus available consumer compatibility fixtures and is not blocked
on publication of every companion package. `workspace_optimization_complete` additionally requires
the exact pyELK, pyHermiT, projector, Exact, and OAEI revisions to pass the workflow matrix. An open
workspace decision does not prevent a correct core release with scalar consumer fallback, but no
documentation may claim the multi-consumer performance objective or zero-materialization native
workflow until it closes.

## 11. Versioning and migration

This specification-only change does not claim the redesign is implemented and
does not itself change package, API, model, wire, or adapter version constants.
Retained native storage is private and need not change public model semantics.

`EncodedStructuralView` is an additive public capability with its own schema.
Before WP17 advertises it, the implementation change MUST record the package/API
and adapter compatibility decision, generate and audit its schema ledger, update
the public API snapshot, and coordinate consumer ranges. Model schema changes
only if structural equality/fingerprints change. Wire version changes only if
stable wire sections or meaning change; sharing an internal layout is not a
reason to reinterpret existing wire bytes.

WP17 is the sole owner of the encoded-view schema version and of any required
`API_VERSION` or `ADAPTER_PROTOCOL_VERSION` decision/implementation. It records
unchanged values explicitly and MUST NOT bump project/package `__version__`.
WP18 is the sole owner of the package SemVer release bump in `pyproject.toml`
and `pyowl_core.__version__`, plus `CHANGELOG.md` and `MIGRATION.md`; it consumes
WP17's frozen ledger and MUST NOT reinterpret its API/adapter/encoded-schema
values. If release integration discovers that ledger is wrong, work returns to
a reviewed WP17 contract amendment instead of changing both version families
in WP18.

A snapshot/provider lacking the encoded capability remains valid for scalar
consumers. A performance consumer may require it explicitly and fail with
`AdapterCompatibilityError`; it cannot reparse the path to manufacture it.

## 12. Program acceptance

### 12.1 Core package release eligibility

The retained-native core may be released only when:

- every required syntax and OWL constructor passes Python/native differential,
  W3C, generated, round-trip, hostile-input, fuzz, and consumer suites;
- native document and closure loads retain native storage without eager
  ontology-sized Python materialization, proven by object/allocation counters;
- the independent encoded-view decoder and available consumer compatibility
  fixtures receive the same native-backed snapshot identity without parser,
  resolver, encoder, decoder, scalar-materialization, or ontology-sized-copy
  counters changing;
- direct, decoded, mmap, overlay, and composite views have equal public
  behavior and fingerprints;
- pure wheels remain complete on Python 3.10+ and forced-native wheels contain
  no Java dependency;
- query-ready native loading meets at least the Horned-equivalence gate in
  `performance.md`, with the faster-than-all-comparators result retained as the
  optimization target; and
- evidence includes raw samples, environment, exact comparator versions,
  corpus hashes, phase profiles, peak RSS, and correctness outputs.

### 12.2 Workspace optimization completion

The stronger workflow objective is complete only when the exact companion
revisions named in section 10 pass the same direct/mmap/overlay/composite
snapshot through projector, pyELK, pyHermiT, Exact, and OAEI with no repeated
parse, scalar ontology expansion, wire round trip, or base flattening. The three
native compiler packages must consume the encoded view in coarse calls; Exact
and OAEI must remain public-API compatibility consumers. This status gates the
multi-consumer performance claim, not publication of an otherwise eligible
core package.

No performance result can waive semantic completeness, deterministic output,
resource limits, security controls, packaging portability, or the pure-Python
fallback.
