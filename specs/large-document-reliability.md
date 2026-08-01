# Large-document canonicalization and RDF diagnostic reliability

Status: normative successor contract for the `0.2.0` line. It refines the
anonymous-individual, parser-diagnostic, resource-limit, native-parity, and
benchmark contracts after failures observed by a downstream biomedical
consumer against `pyowl-core==0.1.1` (`b0d8fd27537b2f177cfe9a5e0fd41f33b9f18f19`).

Downstream incident evidence is pinned to PyMatcha commit `438af9e`
(`fix(ontology): preserve RDF/XML blank-node components`), specifically its
`docs/bioml-live-data-incidents.md`. The runbook's moving branch is not
normative. Four ontology inputs reproduce the aggregate-work defect: NCIt,
FMA, and SNOMED CT in both RDF/XML and Functional Syntax. Exact reproduction
data and evidence qualifications are in appendix A.

## 1. Scope and required outcomes

This change set has five outcomes:

1. disconnected anonymous-individual graphs are canonicalized independently
   without weakening document-wide resource bounds;
2. every configured resource-limit failure is classifiable from typed fields;
3. strict RDF mapping failures carry the already-computed bounded mapping
   report, so diagnosis never requires a second partial parse;
4. missing RDF reification main triples carry bounded structural evidence; and
5. the existing partial RDF mapping behavior is promoted through public load
   configuration only under the diagnostic-only rules below.

The change MUST NOT introduce a segmented document API, relax strict RDF
mapping, infer declarations for ambiguous predicates, or increase
`max_canonical_work` to conceal aggregate work. Consumer-side RDF/XML chunking
is not a supported substitute for a complete ontology document.

Streaming or segmented parse APIs remain out of scope. Four failures across
two serializations show aggregate work rather than a demonstrated hard
component: the SNOMED RDF/XML preflight's largest XML-level component is 216
top-level nodes, while FMA and NCIt recover only by composing 300 and 343
independent chunks. WP23 MUST first measure the post-mapping structural graph;
only evidence that one structural component independently exceeds the budget
can justify a separately reviewed canonical-algorithm scope expansion.

Serialization choice is not a mitigation. SNOMED fails in Functional Syntax
and RDF/XML; reserializing 211 MB of Functional Syntax as 921 MB of RDF/XML is
approximately a 4.4-fold size increase that merely reaches a syntax a consumer
can currently split. User guidance MUST NOT recommend this as a workaround.

## 2. Verified baseline

The Python and Rust implementations both perform anonymous canonicalization
after syntax parsing and RDF-to-structural mapping. The Rust source is not an
opaque dependency: `native/src/parse/anonymous.rs` implements the same global
structural blank graph, refinement, canonical order, document scope, and
anonymous-key domains as the Python reference. Instrumentation is still
required to identify the charged term in a live incident, but implementation
MUST NOT be blocked on rediscovering this layer boundary.

In model schema 1, all labels and arcs in a document are presented to one
canonical-label problem, even though a `BlankNodeArc` connects occurrences
within one structural root and roots are joined only when they share a blank
label. A document with many independent small components can therefore exhaust
the candidate-order budget as if it were one symmetric graph. The Python path
also solves the global order twice: once for provisional scope derivation and
again for final keys.

### 2.1 Measured XML-level component locality

PyMatcha commit `438af9e` records a structural preflight over the
checksum-pinned 921,509,982-byte SNOMED RDF/XML source before consumer
chunking:

| Measure | Observation |
|---|---:|
| connected components over top-level nodes sharing `rdf:nodeID` | 631,630 |
| largest component | 216 top-level nodes |
| largest component span | 216 top-level nodes |
| maximum concurrently active components | 1 |
| document-order contiguity | every measured component contiguous |

This is strong evidence for many local problems rather than one hard graph,
but it is deliberately not mislabeled. The preflight measures connected
components of top-level RDF/XML nodes sharing `rdf:nodeID`, a consumer-visible
over-approximation of the post-mapping structural graph emitted by
`_blank_arcs`. The structural-model counterpart is a required WP23 measurement
and decides whether per-component budgeting alone closes each incident.

## 3. Component canonicalization scheme v2

### 3.1 Component partition

Freeze MUST scan every structural root and form connected components with a
bounded union-find or equivalent linear algorithm:

- every anonymous source label is a vertex;
- labels occurring in the same structural root are unioned;
- a root containing one anonymous label belongs to that label's component;
- roots without anonymous labels bypass this algorithm; and
- every arc, root skeleton, and occurrence is assigned to exactly one
  component.

The component graph is complete for identity: its canonical bytes include
root kind, complete non-blank skeleton, occurrence roles, arc direction and
multiplicity needed to reproduce the component's alpha-equivalence class.
Implementations MUST NOT discard root multiplicity before distinct anonymous
individuals have received keys.

Partitioning, component manifests, and sorting remain charged to the existing
document-global `max_terms` and temporary-memory/deadline limits. The
implementation MUST check the document-wide sum of labels and arcs before
starting component refinement. Moving refinement to components does not turn
`max_terms` into a per-component allowance.

### 3.2 Canonical component classes and multiplicity

Each connected component is refined and canonically ordered independently.
`max_canonical_work` applies separately to each component; its setup,
refinement, and candidate-order charges retain the schema-1 accounting formula
unless a later model-schema change replaces it. One pathological component
therefore still fails.

Components are grouped by their complete canonical component graph bytes, not
by a digest alone. The document component manifest is the lexicographically
sorted sequence of:

```text
(component_graph_length, component_graph_bytes, multiplicity)
```

Multiplicity is semantic. Two disconnected, isomorphic components cannot
share all anonymous keys merely because their roots would become equal after a
collision. For example, two class assertions about two distinct anonymous
individuals MUST remain two assertions.

For an equivalence class with multiplicity `q`, canonical component occurrence
ordinals are `0 .. q-1`. An anonymous key is derived from at least:

```text
document_scope
component_graph_digest
component_occurrence_ordinal
component_local_canonical_index
```

with unambiguous length framing and the scheme-v2 domain. Complete graph bytes,
not a digest comparison, determine equivalence-class membership.

There is no label-free way to associate source components that are exactly
graph-isomorphic with otherwise indistinguishable ordinal slots. An
implementation MAY use source labels solely to choose that internal
association after the equivalence class and all output slots are fixed. This
exception is valid only because any such association permutes an identical set
of component results. Source labels MUST NOT enter component graph bytes,
document scope, anonymous keys, fingerprints, or canonical output. Property
tests MUST prove output invariance under arbitrary blank-label, root-order, and
component-order permutations. Outside an exact component equivalence class,
source label, parse order, object address, and hash iteration remain forbidden
tie-breakers.

### 3.3 Scope and key derivation

The logical two phases are:

1. compute canonical component graphs and local orders under a provisional
   context; then derive `document_scope` from the ontology key and sorted
   multiplicity-preserving component manifest; and
2. derive final anonymous keys from that scope, component class/occurrence, and
   local index, then freeze roots.

Implementations SHOULD reuse phase-one component graphs and orders. Repeating
partition refinement merely because the scope became known is not required and
cannot alter the result. Parallel component processing is permitted, but
publication order and bytes MUST be identical to serial execution.

All anonymous identity domains whose interpretation changes are versioned
together, including document scope, snapshot document scope/rescoping,
anonymous key, blank graph, blank color, provisional scope, and the new
component-manifest/class domains. The exact byte strings and framing are frozen
in the model-schema-2 ledger and shared by the independent reference
implementation.

## 4. Structured resource-limit failures

Every `ResourceLimitError`, irrespective of operation or backend, MUST expose:

```python
limit: str
observed: int | float
allowed: int | float
details: Mapping[str, str | int | bool]
```

The fields are never `None` for a configured-limit failure. Actual allocator
failure remains `MemoryError`; an internal invariant failure is not mislabeled
as a caller limit. Message wording is non-contractual and consumers MUST NOT
parse it.

The native error frame carries these values as typed fields through its single
Python conversion boundary. A bridge MUST NOT infer them from a message. For
`max_canonical_work`, details include the stable keys
`component_count`, `largest_component_labels`,
`largest_component_arcs`, `refinement_rounds`, and `work_term`; values not yet
observed are omitted rather than invented. `work_term` is one of `setup`,
`refinement`, or `candidate_orders`. Details are immutable, bounded, and
mirrored by `as_diagnostic()`.

The need is empirically confirmed. Under the same named limit, initial NCIt,
SNOMED, and FMA parses reported `NATIVE_WIRE_LIMIT` with “native anonymous
canonicalization exceeds max_canonical_work”, while an FMA retry after
reification cleanup reported the same code with “native operation exceeds
max_canonical_work”. Every enforcement site MUST construct identical
`(limit, observed, allowed)` fields regardless of call site or reparsing after
caller modification. A dedicated regression drives both paths and compares
typed fields; message text is intentionally ignored.

Python/native differential tests compare `(code, limit, observed, allowed,
details)` for equivalent failures. The equality requirement applies to the
fields a backend can have reached before the limit, not human wording.

## 5. RDF mapping and reification evidence

### 5.1 Strict mapping

Strict RDF/XML and Turtle mapping remains the default. Before raising
`UnsupportedSyntaxError(code="RDF_MAPPING_INCOMPLETE")`, a backend MUST finish
the bounded report over the graph ledger and attach it as
`error.rdf_mapping_report`. The report contains `total_triples`,
`consumed_triples`, `conformant=False`, mapping rule IDs, deterministic
diagnostics, and at most `max_diagnostics` unconsumed examples.

Each `RDFTripleEvidence` exposes `subject`, `predicate`, `object`, and
`object_kind`, where `object_kind` is exactly `"iri"`, `"blank"`, or
`"literal"`. Evidence strings obey diagnostic size and redaction bounds. The
same report fields and order are returned by Python and native backends. A
consumer can therefore identify all reported predicates and subjects from the
strict exception without rerunning a partial parse.

Native-only codes such as `NATIVE_RDF_MAPPING_INCOMPLETE` are internal. The
public boundary normalizes them to `RDF_MAPPING_INCOMPLETE`.

### 5.2 Missing reification main triple

An `owl:Axiom` or nested annotation reification whose asserted main triple is
absent remains an `UnsupportedSyntaxError`. Its diagnostic details MUST carry,
where present:

```text
reification_subject
annotated_source
annotated_property
annotated_target
annotated_target_kind
main_triple_present = false
```

Values are bounded and sanitized. Missing or ambiguous metadata is represented
by omission plus a stable diagnostic code, never guessed. Python and native
backends expose the same detail keys. This evidence distinguishes a malformed
source document from damage introduced by an external document splitter.

The evidence sequence is capped by `max_diagnostics` and reports total,
retained, and suppressed issue counts so removals can be reconciled against
retained axioms without pretending the bounded sample is exhaustive.

This is a required, not recommended, deliverable. The pinned FMA incident
removed 90 whole-document orphan `owl:Axiom` wrappers and another 107,588
wrappers from chunked loads after missing-main-triple failures. WP23 eliminates
the chunk-caused removals, but the 90 invalid-export cases remain auditable.
Neither a consumer composite's `is_complete` flag nor successful publication
is evidence of base-triple or axiom-annotation parity.

## 6. Partial RDF mapping policy

`LoadOptions` in API line `(0, 2)` includes
`allow_partial_rdf_mapping: bool = False`. It applies only to RDF/XML and
Turtle one-document diagnostic parsing. When true, `parse_document` may return
an `OntologyDocument` with `rdf_mapping_report.conformant == False`; every
unmapped statement is explicitly counted as dropped and bounded evidence is
retained.

`load_snapshot` and `coerce_snapshot` MUST reject this option with
`OptionConflictError(code="PARTIAL_RDF_MAPPING_SNAPSHOT_FORBIDDEN")` whenever
acquisition would parse bytes. They also reject an already parsed
nonconformant document. A nonconformant document is never passed to a reasoner,
encoded as a valid snapshot, cached as conformant, or selected by defaults.
For OWL/XML and Functional Syntax, setting the option raises
`OptionConflictError(code="PARTIAL_RDF_MAPPING_FORMAT_CONFLICT")`.

This deliberately narrow public surface supports diagnostics without making
silent data loss a loading mode.

## 7. Compatibility and version ledger

The coordinated target is:

| Version | Target | Reason |
|---|---:|---|
| package | `0.2.0` | public API and canonical identity change |
| `API_VERSION` | `(0, 2)` | structured errors/report and `LoadOptions` field |
| `MODEL_SCHEMA_VERSION` | `2` | anonymous scope/key/fingerprint semantics |
| `WIRE_FORMAT_VERSION` | `(1, 2)` | optional `ENCODED_STRUCTURAL_V2` is added without changing required v1 sections |
| `ADAPTER_PROTOCOL_VERSION` | `1` | handshake shape is unchanged; supported model/schema values change |
| encoded structural schema | `2` | schema 1 is frozen and pins model schema 1/canonical-model-v1 bytes |

Wire minor 2 assigns optional section kind `0x8004` to
`ENCODED_STRUCTURAL_V2`; kind `0x8003` remains permanently bound to schema 1.
Model-schema-1 snapshots, anonymous canonical bytes, fingerprints, and
consumer caches are rejected or
regenerated. Non-anonymous model canonical bytes remain unchanged, while any
document/snapshot fingerprint whose preimage includes the model schema changes
as specified. Consumers advertise model schema 2 and encoded structural schema
2 explicitly; schema-1 readers never decode schema-2 columns as schema 1.

## 8. Verification and incident evidence

Required repository-owned tests and evidence are:

- two or more disconnected isomorphic anonymous components remain distinct,
  while duplicate roots still follow the normal canonical-set rule;
- property tests permute source labels, roots, components, hash seed, and
  backend and obtain identical model-schema-2 bytes/fingerprints;
- charged canonical work is the sum of recorded component work, with
  `max_canonical_work` enforced per component and document-global term/memory
  limits retained;
- one oversized connected component raises a fully structured limit error;
- the Python and forced-native backends pass identical canonical, error, strict
  mapping, and reification evidence fixtures;
- a pinned DOID RDF document exposes its unconsumed predicate/subject evidence
  from the strict exception in one parse; and
- native phase telemetry proves whether setup, refinement, or candidate order
  dominated each large-document incident.
- the post-mapping `_blank_arcs` graph is measured for NCIt, FMA, SNOMED
  RDF/XML, and SNOMED Functional Syntax, reporting connected-component count,
  maximum labels/arcs/roots per component, maximum document-order component
  span, and maximum simultaneously open component intervals. “Open” means a
  component whose first root has been seen but whose last root has not; it is
  an ordering diagnostic, not a claim that freeze is streaming.

Large corpus acceptance follows `performance.md`. A normative manifest entry
requires a complete SHA-256 or a license-controlled local manifest pin, stable
acquisition locator/revision, license, and redistribution policy. Under default
`ParseLimits`, each available pinned input MUST load as one document, with no
consumer chunking and no `ResourceLimitError`:

| Input | Bytes | Incident baseline | Current chunks |
|---|---:|---|---:|
| NCIt `Thesaurus.owl` | 747,403,746 | 269.68-s load; 4,375,028-KiB peak RSS; 4:31 wall | 343 |
| SNOMED RDF/XML | 921,509,982 | 361.989-s load; 3,016,768-KiB peak RSS; 17:37.97 wall | 363 |
| FMA `fma.owl` | 208,047,132 | 131.717-s load; 2,520,932-KiB peak RSS; 7:08.81 wall | 300 |
| SNOMED Functional Syntax | 211,564,833 | failed at 32.02 s and 3,066,088-KiB peak RSS; no recovery path | n/a |

For each RDF/XML input, same-machine peak RSS MUST be at or below its chunked
baseline. The Functional Syntax input MUST load below the failed-run RSS
observation; its failed elapsed time is ordering evidence, not a throughput
gate. The Functional input proves the correction is post-mapping structural
rather than a consumer RDF/XML workaround.

The SNOMED RDF/XML totals of 1,818,750 axioms and 386,116 declarations and FMA
totals of 791,162 axioms and 104,942 declarations are regression anchors where
the composed workaround is the only available reference. They are not parity
oracles. Counts, signature, and an independent model-1-to-model-2
alpha-equivalence oracle remain required on corpora for which an unmodified
raised-limit baseline completes.

Licensed SNOMED lanes are mandatory for the private incident-closure decision
when authorized data is available, but cannot be the sole public release gate.
NCIt, FMA, and a redistributable/generated Functional Syntax corpus with the
same component-scaling property provide public gates. Timing/RSS figures are
single-machine regression anchors; raw samples and exact tooling are retained
and no portable performance claim is derived from them.

The pinned DOID expectation is 305,919 total triples, 305,901 consumed, and 18
unconsumed across two predicates. The first strict exception MUST expose the
bounded evidence without the 39.257-second partial-mode reparse.

## 9. Delivery order

The workpackages implement the contract in this order:

```text
WP19 structured limit errors and telemetry
  |
  +--> WP20 strict RDF mapping evidence
          |
          +--> WP21 reification evidence
                  |
                  +--> WP22 partial mapping public policy
                  |
                  +--> WP23 component canonicalization v2
```

WP20 and WP21 are serialized because both edit the native RDF mapping ledger;
their product goals are independent but their file ownership is not. WP23
lands the package/model/encoded-schema version transition only after the
diagnostic instrumentation and RDF error evidence are available. The partial
mapping option is required for the `(0, 2)` public contract selected here, but
remains a diagnostic-only leaf rather than a prerequisite for the
canonicalization algorithm.

## 10. Downstream handoff

After WP23 passes its one-document gates, PyMatcha may retire
`_load_rdfxml_in_chunks`, `_scan_rdfxml_chunks`, `_iter_rdfxml_chunks`, its
duplicate component tracking, chunk-level reification guard, and substitution
of a larger RDF/XML source for a Functional Syntax source. Those removals are
consumer-owned and outside this repository. The same handoff retires the
consumer's self-flagged unbounded-prologue and scan/emit-mutation hardening
gaps rather than promoting them into a core segmented-parser API.
Whole-document reification validation and any explicit DOID declaration repair
remain: they address source-data defects rather than pyOWLCore's aggregate
canonicalization defect.

## Appendix A — reproduction data

The parser baseline is `pyowl-core==0.1.1`, tag `v0.1.1`, commit
`b0d8fd27537b2f177cfe9a5e0fd41f33b9f18f19`. The installed wheel was verified
against its `RECORD`: 107 files and no mismatches. All workarounds in the
incident observations are therefore consumer-side.

Every recorded run used forced native backend, offline deterministic loading,
and `ImportPolicy.IGNORE` because PyMatcha's `match_imports` default was false.

| Input | Bytes | SHA-256 | Ontology IRI |
|---|---:|---|---|
| NCIt `Thesaurus.owl` | 747,403,746 | `1a7182a7327ebc4181f7d6b0f7e81ed04dd258f1a86bd8f560e4a0d61439d58a` | `http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl` |
| FMA `fma.owl` | 208,047,132 | `beb3dc47979ad5434ef70fd02af4307f147f2023f7d8c2c57103b995191194c3` | `http://purl.org/sig/ont/fma.owl` |
| SNOMED RDF/XML | 921,509,982 | licensed; exact digest pinned in the private local manifest | `http://snomed.info/sct/900000000000207008` |
| SNOMED Functional Syntax | 211,564,833 | licensed; exact digest pinned in the private local manifest | none |
| DOID `doid.owl` | 28,385,948 | `611355c445537fcf4bae2c519f1b3598af5a8fea793274316e35525b7d05e945` | `http://purl.obolibrary.org/obo/doid.owl` |

The DOID mapping report contains four triples for undeclared predicate
`OBI_9991118` and fourteen for `oboInOwl#created_by`. After the consumer's
in-memory declaration repair, the incident run retained 177,912 axioms. The
core does not adopt that repair; it only makes the strict failure auditable in
one pass.

All timings and RSS values in this document are single observations on one
machine. They are ordering evidence and same-machine regression anchors only,
not portable benchmark claims.
