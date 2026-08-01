# Large-document canonicalization and RDF diagnostic reliability

Status: normative successor contract for the `0.2.0` line. It refines the
anonymous-individual, parser-diagnostic, resource-limit, native-parity, and
benchmark contracts after failures observed by a downstream biomedical
consumer against `pyowl-core==0.1.1` (`b0d8fd27537b2f177cfe9a5e0fd41f33b9f18f19`).

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

Large corpus acceptance follows `performance.md`. A normative manifest entry
requires a complete SHA-256, stable acquisition locator/revision, license, and
redistribution policy; a truncated digest such as `1a7182…d58a` is only an
incident note. The initial NCIt observation used a 747,403,746-byte
`Thesaurus.owl`; the exact artifact is not normative until its complete digest
and provenance are recorded. A legally available, pinned NCIt artifact MUST
load in one pass with default limits and lower same-machine peak RSS than the
documented 4,375,028-KiB chunked incident path. Counts, signature, and an
independent model-1-to-model-2 alpha-equivalence oracle must match a
raised-limit baseline.

The initial DOID observation reported 18 unconsumed triples out of 305,919,
across two predicates, and a 39.257-second second diagnostic parse. These
values become golden expectations only after the exact source bytes are pinned;
the required invariant is that the first strict exception contains the bounded
evidence and no second parse occurs.

Licensed SNOMED Functional Syntax is a private incident-closure lane and SHOULD
load its pinned 211,564,833-byte artifact in one pass. It cannot be the sole
public release gate. A redistributable or generated Functional Syntax corpus
with the same disconnected-component scaling property is the public gate.
Timing and RSS comparisons use the same machine/tooling and publish raw
samples; the incident RSS number is not a portable absolute threshold.

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
consumer-owned and outside this repository.
Whole-document reification validation and any explicit DOID declaration repair
remain: they address source-data defects rather than pyOWLCore's aggregate
canonicalization defect.
