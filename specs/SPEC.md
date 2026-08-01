# pyowl-core — master specification

Status: normative design for the first conformant implementation. The words
MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are requirements in the sense of
RFC 2119. A package built from the current scaffold is not yet conformant.

## 1. Product definition

`pyowl-core` is the Java-free structural foundation shared by Python ontology
applications in this workspace. It parses an OWL ontology once, preserves the
complete OWL 2 structure, resolves imports under an explicit policy, and makes
an immutable snapshot reusable in the same process or through a validated
binary wire snapshot.

The distribution is `pyowl-core`, the import package is `pyowl_core`, and the
minimum Python version is 3.10. Original project source is under Apache License
2.0; every artifact accurately reports and carries notices for linked or
bundled third-party code. A release MUST install and operate without a JDK, JRE, JVM,
OWLAPI, JPype, ROBOT, Maven, Gradle, `.jar`, or downloaded Java artifact.

The model baseline is the W3C OWL 2 Structural Specification, not an RDF triple
store and not the object model of one reasoner. RDF is an exchange syntax;
reasoner index/tableau state is a derived product.

## 2. Why this package exists

Without a common kernel, Exact-OM, pyELK, pyHermiT, projection, and evaluation
would each read the same large biomedical ontology, build incompatible Python
objects, and retain duplicate indexes. The contract here permits:

1. standalone use from a path, bytes, or stream;
2. an Exact-OM run to hand the same `OntologySnapshot` identity to consumers;
3. consumers to cache their own derived IR by shared fingerprints; and
4. process boundaries to use the stable wire format rather than reparsing a
   path or serializing arbitrary Python objects.

Parsing once does not imply compiling once. Each reasoner or projector still
builds only the derived representation its algorithm requires.

## 3. Scope

The first stable release includes:

- every OWL 2 structural constructor in [`model.md`](model.md), including
  annotations, plus a separately namespaced optional SWRL/DL-safe-rule
  extension so rules are never misrepresented as OWL 2 axioms;
- standards-based canonical structural equality and deterministic hashing;
- immutable single documents, resolved import-closure snapshots, deltas, and
  persistent overlays;
- readers for RDF/XML, Turtle, OWL/XML, and Functional-Style Syntax;
- deterministic writers for RDF/XML and Functional-Style Syntax, with Turtle
  and OWL/XML writers before 1.0 unless a documented release amendment moves
  them;
- explicit import resolution with local catalogs, mappings, offline mode,
  resource limits, provenance, and cycle handling;
- signatures, declaration/annotation/axiom indexes and reusable structural
  views, all lazy and thread-safe;
- canonical fingerprints and a stable versioned wire/cache format;
- adapter contracts for Exact-OM, pyELK, pyHermiT, projectors, and evaluators;
- a complete pure-Python implementation plus a semantically equivalent,
  retained-native Rust ontology engine behind the same public contracts; and
- conformance, differential, fuzz, security, packaging, and performance gates.

Support for RDF-star syntax is not part of OWL 2 and is outside 1.0. RDF 1.2
input MAY be added only with explicit downgrade/error rules. OBO syntax MAY be
an external adapter but is not a core 1.0 parser requirement.

## 4. Non-goals

The package MUST NOT:

- classify, realize, check consistency, answer semantic entailment, repair an
  ontology, or claim reasoner completeness;
- embed ELK/HermiT normalization, clausification, saturation, dependency sets,
  blocking, taxonomy, or tableau state;
- define OWL meaning by the quirks of one parser or Java implementation;
- expose Rust/PyO3 objects, RDFLib nodes, Horned-OWL objects, private native
  arena IDs, or a parser-specific graph as stable public values (schema-local
  IDs inside the documented encoded structural/wire formats are permitted only
  under their owner and version contracts);
- silently fetch imports, guess a security-sensitive policy, or hide an
  unresolved import;
- use pickle for trusted or untrusted interchange;
- promise byte-for-byte preservation of source formatting; or
- make a native compiler necessary to install a working wheel from the sdist.

## 5. Normative architecture

```text
path / bytes / stream / already-loaded provider
                       |
             parse_document (one document; no import fetch)
                       |
                 OntologyDocument
                       |
       resolver + ImportPolicy + ParseLimits
                       |
                  load_snapshot
                       |
         immutable OntologySnapshot / Overlay
             |          |          |          |
       Exact views  pyELK IR  pyHermiT IR  projector IR
             |          |          |          |
       matching     saturation    tableau       edges
```

`OntologyDocument` records direct import declarations. `load_snapshot` is the
only operation in this pipeline that constructs a resolved closure. A snapshot
retains document boundaries and an import-resolution manifest; it is not the
unordered union of anonymous-individual-bearing documents.

Detailed dependency and ownership rules are in [`architecture.md`](architecture.md).
The exact public signatures are frozen in [`contracts.md`](contracts.md).

## 6. Required public workflow

```python
document = parse_document(data, format="turtle", document_iri=base)

snapshot = load_snapshot(
    document,
    options=LoadOptions(imports=ImportPolicy.RESOLVE_STRICT),
    resolver=resolver,
)

# Identity-preserving in-process communication. Both values implement OntologyView.
assert coerce_snapshot(snapshot) is snapshot
assert coerce_snapshot(exact_source) is exact_source.owl_snapshot()

# Persistent repair trial without copying the base closure.
trial = apply_delta(snapshot, OntologyDelta(add_axioms=frozenset({mapping})))

# Cross-process/cache communication; never pickle.
payload = encode_snapshot(snapshot)
equivalent = decode_snapshot(payload)
mapped = open_snapshot(cache_path, mmap=True)
```

Consumers that support repair overlays MUST accept `OntologyView` directly;
snapshot-only operations MAY require concrete `OntologySnapshot`. A standalone convenience API
MAY additionally accept a path or bytes, but it MUST call `coerce_snapshot` and
MUST NOT contain an independent OWL parser.

## 7. Global invariants

### 7.1 Structural completeness

The common model can represent all OWL 2 structural objects and axiom
annotations without loss. Parsing a construct unsupported by a particular
consumer is not a core parse failure; the consumer's compiler returns a stable
profile/unsupported-feature diagnostic.

### 7.2 Immutability and identity

All public structural values, documents, snapshots, deltas, and overlays are
immutable and hash-safe. A snapshot passed in-process is returned by identity,
not cloned. Lazy caches may mutate privately under synchronization but cannot
change observable content or fingerprints.

### 7.3 One semantic identity

Equality follows OWL structural equivalence plus the canonicalization rules in
[`model.md`](model.md). It never varies by backend or consumer. Compatibility
quirks, such as a legacy ELK key retaining language-tag case, live in consumer
adapters. Source maps may preserve the original token spelling without making
it part of semantic identity.

### 7.4 Determinism

Given equal logical inputs, options, model schema, and import-resolution
manifest, construction order, hash seed, thread schedule, backend, source
syntax, prefixes, and filesystem path do not affect canonical fingerprints or
wire output. Iteration is either explicitly canonical or explicitly documented
as unspecified; no consumer may depend on unspecified order.

### 7.5 No hidden work

Import network access, materializing an overlay, building a nontrivial index,
or copying/mapping a wire buffer is observable through diagnostics/metrics.
Queries never cause import fetching. `parse_document` never resolves imports.

### 7.6 Native parity

The Python backend is the semantic reference and complete fallback. Rust is an
optimization with byte-for-byte canonical/wire and diagnostic parity where the
contract specifies bytes/order, and normalized semantic parity elsewhere.
There is no native-only OWL feature. A selected native operation retains its
complete immutable ontology in native storage behind the public Python facade;
it does not eagerly rebuild an ontology-sized Python object graph. Scalar
objects are materialized lazily, while bulk consumers use documented encoded
structural views. See `native-ontology-redesign.md`.

### 7.7 Resource safety

All parsers, resolvers, wire readers, writers, indexes, and overlay operations
obey caller-configurable limits. Untrusted bytes cannot request allocation or
recursion before lengths/counts are validated. See [`security.md`](security.md).

## 8. Version and compatibility policy

Four versions are distinct:

- package/API version: SemVer (`pyowl_core.__version__` and `API_VERSION`);
- `MODEL_SCHEMA_VERSION`: canonical model/equality/fingerprint semantics;
- `WIRE_FORMAT_VERSION = (major, minor)`: independent binary format version;
- `ADAPTER_PROTOCOL_VERSION`: snapshot-provider and plugin handshake version.

A change to public names or observable behavior follows SemVer. Changing
canonical equality, blank-node scoping, or fingerprint inputs increments the
model schema. A wire major change is incompatible; a minor change can add only
skippable optional sections or backwards-compatible features. Every consumer
cache key includes its own compiler schema in addition to core versions and the
appropriate snapshot fingerprint.

The reviewed `0.2.0` successor transition is defined in
[`large-document-reliability.md`](large-document-reliability.md): API line
`(0, 2)`, model schema 2, multiplicity-preserving component-scoped anonymous
canonicalization, and encoded structural schema 2. Wire and adapter versions
remain independent and follow that document's explicit version ledger.

Readers MUST reject an unknown required wire feature and MUST NOT reinterpret a
new model schema as the old one. Cache readers rebuild on supported
incompatibility; explicit IPC calls raise `WireVersionError`.

The 0.x API may refine names only through reviewed spec changes that update all
workspace consumers together. The 1.0 boundary freezes the public model and
wire v1 reader for at least the 1.x line.

## 9. Backend and dependency policy

The default `backend="auto"` selects the verified private Rust extension when
available. If unavailable or self-test-incompatible, it selects the complete
Python backend and emits `NativeBackendUnavailableWarning` once per process,
including the selected backend and remediation. `backend="python"` is an
explicit choice and emits no fallback warning. `backend="native"` raises
`BackendUnavailableError` rather than falling back. Native capability is
advertised only for a complete top-level operation that ends in a retained-
native document/view; parsing natively and then silently reconstructing the
complete Python model is not the optimized native contract.

Horned-OWL is an implementation/reference candidate for Rust parsing because
its model follows OWL 2, but it is behind an internal adapter. Its LGPL and any
transitive obligations require release-blocking legal/license review. The
preferred shipping outcome is a clean-room Apache-compatible implementation
that keeps the whole artifact under Apache-2.0; linking Horned-OWL is the
reviewed fallback and then requires accurate multi-license artifact metadata,
notices, source/relinking compliance, and user documentation approved by that
review. The Apache project license never masks third-party terms. A
Horned-OWL or `py-horned-owl` public type can never escape. Adopting or updating
it requires capability, conformance, license, MSRV, and performance evidence;
the core owns any missing standards behavior.

Core runtime dependencies remain minimal. Entry-point plugins are never loaded
implicitly when parsing untrusted data. All dependencies and generated wheels
must pass the Java-artifact and license scans in [`packaging.md`](packaging.md).

## 10. Consumer boundary

| Consumer | Receives | Builds privately | Forbidden duplication |
|---|---|---|---|
| Exact-OM | snapshot/overlay | matcher-specific `KnowledgeSource` views | parser, structural records, projection algorithm |
| pyELK | OWL structural closure | EL profile report, indexed EL expressions, saturation/taxonomy state | own OWL value model/parser |
| pyHermiT | resolved OWL 2 DL closure | normalization, role automata, DL clauses, tableau/classification state | own OWL value model/parser |
| pyOwl2Vec-Star-projector | snapshot/overlay | projection plan, encoded edge buffers | parser or reasoner IR |
| OAEI evaluator | source/target snapshot and mapping overlays | metric/reasoner sessions | ROBOT/DeepOnto/OWLAPI path round trips |

See [`adapters.md`](adapters.md) for exact adapter rules.

## 11. Specification map

- [`architecture.md`](architecture.md): layers, ownership, lifecycle, threading.
- [`contracts.md`](contracts.md): frozen public API, protocols, errors, events.
- [`model.md`](model.md): complete OWL structural model and identity rules.
- [`parsing-imports.md`](parsing-imports.md): formats, resolver, provenance.
- [`snapshots-overlays.md`](snapshots-overlays.md): closure, deltas, persistence.
- [`indexes-views.md`](indexes-views.md): shared nonsemantic indexes and queries.
- [`wire-format.md`](wire-format.md): stable cache/IPC schema and validation.
- [`adapters.md`](adapters.md): integrations and extension points.
- [`native-backend.md`](native-backend.md): Rust boundary and Python fallback.
- [`native-ontology-redesign.md`](native-ontology-redesign.md): retained-native
  storage, lazy facade, zero-copy consumer path, comparative goals, and
  successor work plan.
- [`security.md`](security.md): hostile inputs, limits, network and filesystem.
- [`performance.md`](performance.md): biomedical benchmark methodology/gates.
- [`packaging.md`](packaging.md): source/wheels, Python matrix, Java audit.
- [`verification.md`](verification.md): conformance, differential and release gates.
- [`references.md`](references.md): normative and implementation references.
- [`workpackages/manifest.toml`](workpackages/manifest.toml): executable work plan.

If specifications conflict, precedence is: W3C Recommendation for language
semantics and mapping; this master specification; `contracts.md` and
`model.md`; focused specifications; work-package briefs; README/examples.
Conflicts must be corrected, not silently selected by an implementer.

## 12. Definition of done

Version 1.0 requires all of the following:

1. Complete model-constructor coverage is mechanically audited against OWL 2.
2. Required formats pass W3C positive/negative syntax and mapping tests.
3. Import policies, catalogs, cycles, redirects, offline mode, and hostile
   resolver cases pass deterministic tests.
4. Python/native and parse/render/parse differential suites pass.
5. All five workspace consumers pass zero-reparse identity and wire IPC tests.
6. The pure wheel installs with no compiler and passes the full semantic suite
   on Python 3.10 through the supported upper matrix.
7. Native wheels pass parity, sanitizer/fuzz, ABI, and platform tests.
8. Large biomedical benchmark gates meet the targets in `performance.md`,
   including at least Horned-OWL-equivalent query-ready native loading under
   the approved comparative methodology, with no unbounded memory growth.
9. Artifacts, dependency graphs, sdists, wheels, docs, examples, and CI images
   contain no Java runtime/build dependency or bundled Java bytecode/archive.
10. Security/resource-limit and reproducibility gates pass.
11. License/corpus provenance is complete.
12. The provisional PyPI name `pyowl-core` is reserved or confirmed under the
    release owner; the project metadata contains real, reviewed URLs.

## 13. Change control

Any change to a public constructor, canonical identity, fingerprint input,
resolver behavior, wire field, import default, or fallback rule requires:

- a specification change in the same pull request;
- compatibility impact and migration notes;
- new/updated golden and generative tests;
- review by at least one affected consumer owner; and
- an explicit decision about API/model/wire/adapter version increments.

Optimizations may change internal representation only. Measured speed is never
permission to weaken correctness, determinism, safety, or the fallback.
