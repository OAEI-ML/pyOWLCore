# WP14 retained-storage and public-boundary decision

Date: 2026-07-19
Decision status: accepted design input for WP15; performance evidence remains
open
Runtime capability status: not implemented and not advertised by this report

This record freezes the storage and API direction required before the retained
native arena is implemented. It does not change a public version constant,
claim that a retained loader exists, or convert the pre-WP14 performance
artifacts into comparative evidence.

## 1. Retained handle and ownership decision

The native implementation is private. A native-backed public
`OntologyDocument` or `OntologySnapshot` owns one opaque private document or
snapshot handle; no PyO3 class, Rust enum, raw pointer, arena ID, or native
layout enters a public signature. The handle owns its immutable arena. A mapped
handle instead owns a strong reference to the immutable validated mapping and
the descriptor/platform handle needed to keep it stable.

Ownership flows from the public facade to everything borrowing storage:

```text
public document/snapshot facade
              |
              +-- private retained handle -- immutable arena or mapping owner
              |
              +-- scalar value/cache entries (on demand)
              |
              +-- EncodedStructuralView -- read-only buffers/segments
```

An encoded view retains its originating public `OntologyView` as `owner` for
the complete buffer lifetime. Scalar values and dependent indexes must also
keep the necessary storage owner alive. Closing, unmapping, fork handling, and
dependent-view use must therefore fail with the public lifecycle errors rather
than leave a dangling native reference. The extension may borrow an input
buffer only for the duration of a call; retained storage may not contain an
unowned pointer into Python memory.

`coerce_snapshot` and `SnapshotProvider.owl_snapshot()` keep the exact public
object identity supplied by the caller. Native storage ownership is not a
reason to wrap or rebuild an existing view.

## 2. Lazy scalar materialization decision

Native parsing and freeze publish only O(documents + diagnostics + fixed facade
metadata) Python objects before scalar access. Terms, entities, annotations,
expressions, and axioms are materialized as public Python model values only
when a scalar operation asks for them. `iter_axioms()` may materialize rows as
iteration advances, but bulk consumers are not routed through it.

Materialized native-backed values must have the same public type, equality,
hash, canonical ordering, fingerprint semantics, and error behavior as values
from the complete Python backend. Caches are weak, bounded, or governed by the
public index cache policy; a complete scalar scan must not permanently create a
second ontology-sized Python heap. Allocation instrumentation must distinguish
facade/report objects, scalar values, encoded-view metadata, copied structural
bytes, and private native arena bytes.

Capability selection occurs before input is consumed. `AUTO` selects Python
for the complete operation if the requested native capability is incomplete;
forced `NATIVE` raises a public backend/capability error. A native parse
followed by an eager complete Python reconstruction is not a retained-native
load.

## 3. Encoded structural boundary decision

The bulk public boundary is core-owned
`EncodedStructuralView`, requested through
`ontology.view(EncodedStructuralView, schema_version=..., **options)`. Its
schema name is `pyowl-core/structural-columns`; schema versioning is independent
of package SemVer, wire version, and private native layout. Each view reports:

- the schema name/version and exact core model schema;
- an explicit scope/options selection;
- canonical descriptor bytes and structural fingerprint;
- immutable named read-only buffers using documented little-endian exact-width
  columns; and
- a strong `owner`, plus base/delta/member segments for overlay and composite
  views.

Tags come from the model schema ledger, never from Rust discriminants. Dense
IDs are valid only within that retained owner/schema/view and are not semantic
or cache-persistent identities. The Python backend must be able to produce the
same logical buffers, with copying reported explicitly. Native and mapped
storage should publish matching columns directly when their validated layout
allows it. In-process consumers must not encode/decode the stable wire format
merely to acquire this view.

WP14 freezes this direction, not schema 1's row ledger. WP17 alone freezes and
publishes the generated schema/descriptor ledger and records any API or adapter
version decision. Until then `CoreCapabilities.encoded_view_schemas` must not
advertise `pyowl-core/structural-columns`, and documentation must not claim an
encoded-native path.

## 4. Wire and mmap decision

PYOCORE remains the only stable cross-process form. Native arena layout and
encoded-view columns may align with wire columns as an optimization, but they
do not inherit the wire compatibility contract.

`open_snapshot(path, mmap=True)` validates header, section directory, bounds,
references, schemas, and required integrity before publication. The public
snapshot owns the immutable mapping. Dependent scalar values, indexes, encoded
views, overlays, and composites retain that owner. A successful mapped open
must not instantiate one Python object per row or eagerly copy the complete
wire image into a second native arena. Direct, decoded, and mapped views must
have equal public behavior and fingerprints.

Mapped and decoded wire views advertise `wire-v1` and `wire-verified` only
after successful validation. Direct parsed views do not advertise those
features merely because they can later be encoded. The mapped backend is a
storage origin reported through stable diagnostics, not a different semantic
model.

## 5. Capability and diagnostic decision

`CoreCapabilities` remains the only feature-negotiation record and contains
adapter protocol, model schema, wire version, feature names, encoded-view
schema mappings, and a backend diagnostic. Consumers negotiate capabilities;
they do not infer them from `__version__` or import `pyowl_core._native`.

The following names already have normative meaning and are retained:

| Name | Meaning and advertisement rule |
|---|---|
| `retained-native-load-v1` | A complete operation ended in retained native storage without eager ontology-sized Python reconstruction. Advertise only after allocation/correctness gates pass. |
| `ontology-identity-index` | The public ontology identity index is available on direct, overlay, composite, decoded, and mapped views. |
| `wire-v1` | The view came from a supported PYOCORE v1 representation. |
| `wire-verified` | Required wire validation completed successfully. |
| `encoded_view_schemas["pyowl-core/structural-columns"]` | The highest compatible encoded structural schema the view can publish. Omit it until WP17 freezes and implements that schema. |

Backend diagnostic strings describe the selected storage/implementation path;
they are not capability substitutes. Stable provenance may report selected
backend, schema versions, owner kind, materialized scalar rows, copied
structural bytes, and parser/resolver/wire counters once those public
diagnostics exist. It must not report pointers, addresses, private IDs, source
paths, or credentials.

## 6. Comparative timing fences

Resident-byte timing begins immediately before a lane receives the immutable
ontology bytes. File/path acquisition, network I/O, corpus hash preparation,
and import resolver I/O are outside and separately reported. The timer ends
only after the lane publishes its query-ready result and the complete
common-contract ledger/traversal required for that lane. Post-timer work is
limited to comparing already-produced bytes/counts/digests and bounded sample
validation.

Legend: **in** is timed, **out** is separately reported or post-timer, and
**n/a** does not apply to the lane.

| Phase | Python backend | direct retained Rust | installed native wheel | Horned model ready | Horned common contract | py-horned common contract | OWLAPI common contract |
|---|---|---|---|---|---|---|---|
| resident byte receipt | in | in | in | in | in | in | in |
| syntax parse / RDF mapping | in | in | in | in | in | in | in |
| interning / canonicalization / freeze | in | in | in | Horned engine work in | engine + adapter work in | wrapper + engine + adapter in | engine + adapter work in |
| required query-ready indexes | in | in | in | only Horned-model readiness work in | equivalent readiness work in | equivalent readiness work in | equivalent readiness work in |
| document, structural, logical, signature fingerprints and preimages | in | in | in | n/a | in | in | in |
| ontology/document/import identity | in | in | in | n/a | in | in | in |
| diagnostics and bounded provenance | in | in | in | only native Horned result in | reconstruction in | reconstruction in | reconstruction in |
| retained-handle/facade publication | n/a | in | in | n/a | n/a | wrapper publication in | adapter publication in |
| common-ledger full traversal/digests | in | in | in | n/a | in | in | in |
| Python scalar ontology expansion | only if required by readiness; label path | n/a for retained target | n/a for retained target | n/a | forbidden as a shortcut | forbidden as a shortcut | forbidden as a shortcut |
| equality/count/digest assertion | out | out | out | out | out | out | out |
| file acquisition/network/resolver I/O | out | out | out | out | out | out | out |

`horned-model-ready` is an explicitly asymmetric diagnostic lane and never a
Horned-equivalence denominator. Only `common-contract-ready`, including the
independent adapter's mapping, canonical ordering, fingerprint, provenance,
and traversal costs, may be used for the `<= 1.10` aggregate gate. Direct
engine and installed-wheel lanes, resident-byte and file lanes, and fresh- and
steady-process modes remain separate datasets.

For the Python lane, retained-handle/facade publication is `n/a`: the ordinary
Python snapshot and its common-contract ledger still complete inside the
timer, but that completion is not a native publication capability. The
executable scaffold now implements resident-byte and prepared-file
fresh/steady execution. The committed smoke predates the file boundary and
exercises resident bytes only; paired randomized ordering remains
unimplemented and must not be inferred from this timing table.

## 7. Current implementation limitations and evidence status

The redesign was triggered because the post-WP13 native shape is a set of
buffer-returning accelerators around Python-owned ontology storage. The current
artifacts do not demonstrate a retained native document/snapshot, lazy
ontology-sized scalar materialization, direct encoded structural columns, or a
lazy mapped native handle.

The executable scaffold validates the comparator manifest and runs one tiny
generated Functional Syntax input through the core Python common-contract
adapter in fresh- and steady-process resident-byte modes. Contract tests also
exercise the prepared-file modes with a path-independent source-bound document
IRI and equal common-contract digest. The committed smoke exercises the four
fingerprint preimage checks, structural/identity/
provenance/diagnostic inventories, raw-sample recording, and post-timer
equality fence. Every non-Python lane is `not-run`, the reference-machine
record remains `pending`, and `comparative_complete` is false. An intentional
partial invocation succeeds only with the explicit `--allow-partial` opt-in;
the default CLI exit remains fail-closed.

Existing generated-large and biomedical profiles remain historical evidence;
they were not produced by this successor comparative matrix. No numeric
retained-materialization/copy result is asserted here. Required native object
growth, native-to-Python copied rows/bytes, wire/encoded publication copies,
mapped startup allocations, complete phase profiles, and approved-machine
representative samples remain unmeasured under the new contract.

## 8. Not-run blockers

The following are recorded as **not-run**, not passes:

- external runners: direct retained Rust, raw Horned, common-contract Horned,
  py-horned, and OWLAPI each have a separate runner pin; every runner pin is
  `pending` without a runner artifact hash, so all five lanes are non-runnable;
- external steady-process execution: no audited persistent runner lifecycle,
  equal warm-up, or cleanup barrier is implemented;
- installed retained-native-wheel/bulk lane: the scaffold refuses a
  source-tree/native build and no isolated delivered-wheel run is recorded;
- Horned engine inputs: raw and common-contract lanes share the exact
  Horned-OWL 1.4.0 engine artifact pin, but that completed engine pin does not
  complete either separately pending runner pin;
- py-horned and OWLAPI inputs: the py-horned package artifact and OWLAPI
  JDK/GC/heap policy are recorded, but their external runner artifacts remain
  pending;
- file-lane approved evidence and paired randomized blocks: the core file
  boundary is implemented and contract-tested, but external/reference-machine
  file samples and paired ordering are not recorded;
- comparative ratios: executable paired ratio gates are not configured or
  passing;
- fresh/steady NCIT, DOID, medium/list-heavy, and required large RDF/XML
  redesign samples: no approved reference-machine run is recorded;
- retained allocation/copy/RSS counters and full redesign phase profiles: the
  instrumentation/reference-machine run is not recorded; and
- consumer handoff and direct/mmap/overlay/composite workflow matrices: these
  depend on WP17 and companion consumer schemas.

Consequently this decision makes no Horned/OWLAPI parity, query-ready ratio,
RSS, mmap-startup, native-over-Python, or multi-consumer performance claim.

## 9. Dependency exclusion audit

The decision is fail-closed: Horned-OWL, py-horned, OWLAPI, Java archives,
JDKs, and comparator adapters are development benchmark inputs only. They may
not enter runtime/build dependencies, the ordinary test environment, sdist,
pure wheel, native wheel, or installed package payload.

`dependency-audit-shared-host.json` records a limited **pass**. Its source checks
cover dependency metadata/lockfiles, source imports, and package payload
manifests; alias-aware AST checks and bounded packaged-Python inspection close
the corresponding literal-import bypasses. Its SHA-bound artifact checks
inspected the reproducibly built sdist and pure wheel and found no excluded
comparator dependency in either payload. Canonical per-input source hashes and
Git provenance bind the result to clean commit
`f89c9d005698ec969f7a073b0ccea49c801a63f2`; the detached audit JSON is excluded
from the sdist and from its own source identity.

That result does not close the distribution gate. Platform linkage is explicitly
`not-run`; the bounded native marker scan is not a substitute, and no native
wheel was inspected. Release SBOM/license review and approved release packaging
also remain open; the inspected artifacts record missing approved repository,
documentation, and issue metadata as release blockers. WP14 cannot close until
native-wheel platform linkage and payload checks,
comparator-adapter isolation, SBOM/license evidence, and release-packaging
approval are recorded. Absence in an unbuilt working tree is not release
evidence.

## 10. Frozen seams handed to WP15

WP15 may implement behind the following frozen seams without waiting for WP16
or WP17:

1. Opaque private `_NativeDocumentHandle` and `_NativeSnapshotHandle` values;
   the public facade is their sole owner/adapter.
2. A bounded mutable builder that freezes exactly once into immutable shared
   ownership; freeze never converts the complete ontology to Python.
3. Lazy scalar materialization with Python/native value parity and bounded or
   weak caches.
4. Strong lifetime ownership for arena/mapping, scalar values, indexes, and
   future encoded views, including close/fork failure behavior.
5. Allocation/copy counters capable of proving pre-scalar O(documents +
   diagnostics + fixed metadata) Python growth.
6. Separate fail-closed binding registration seams for WP16 ingestion and WP17
   views/indexes/wire. An absent seam advertises no capability.
7. A generated/wire-input test seam that lets WP15 prove arena/facade behavior
   before streaming parsers are available.
8. The complete pure-Python backend and all public semantics remain unchanged;
   `AUTO`/`NATIVE` selection occurs before consuming input.

WP15 must not publish the encoded structural schema, reinterpret PYOCORE wire
bytes, bump package SemVer, or claim comparator performance. Those decisions
remain owned by WP17/WP18 and the evidence gates above.
