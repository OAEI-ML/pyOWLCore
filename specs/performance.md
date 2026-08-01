# Performance and biomedical benchmark contract

Correctness, determinism, safety, and the complete Python fallback are fixed
constraints. Optimization follows measurement and targets end-to-end retained
work, not a parser microbenchmark that then rebuilds Python objects repeatedly.

## 1. Performance objectives

1. Parse/canonicalize each physical ontology document at most once per snapshot.
2. Hand the same in-process `OntologyView` identity to every consumer with no
   ontology-sized allocation.
3. Keep common strings/IRIs/axioms/indexes shared and reasoner IR private.
4. Make retained-native query-ready loading attempt to outperform all pinned
   comparators and, at minimum, remain statistically equivalent to Horned-OWL
   on the required native-engine lane while preserving a usable Python fallback.
5. Make overlays/compositions scale with changed/member metadata, not copied
   bases.
6. Permit mmap wire startup without eagerly materializing all Python nodes.
7. Bound peak RSS/temp/disk and make allocation costs observable.

## 2. Benchmark phases

Every scenario reports phases separately and end-to-end:

```text
acquire bytes (reported, excluded from parser throughput when network)
format detect/decode/lex or RDF parse
RDF-to-structural mapping
canonicalization/anonymous labeling
import scheduling/closure freeze/fingerprints
selected index builds
wire encode/write/decode/mmap
consumer coerce + compile boundary
overlay/composite creation and query
release/GC/unmap
```

No benchmark hides model conversion after the timer. Native-to-public and
wire-to-consumer costs are included in end-to-end measurements. Cold parse,
warm OS cache, parsed-document cache, snapshot wire cache, and mmap modes are
separate results.

Native runs additionally report mutable arena construction, canonical freeze,
retained-handle publication, encoded-view publication, scalar facade
materialization, and Python object counts. A result that serializes to wire or
eagerly expands all rows into Python before becoming query-ready is labeled by
that conversion path and cannot represent the retained-native target.

## 3. Corpus manifest

`benchmarks/corpora.toml` pins exact release URL/repository revision, SHA-256,
format, byte/triple/axiom/entity/import counts, license, acquisition date, and
redistribution policy. CI downloads only through an explicit preparation step
and verifies hashes; nonredistributable data is never committed/published.

Required workload families, with exact versions selected/pinned by WP10:

| Tier | Representative public biomedical ontologies | Purpose |
|---|---|---|
| tiny | generated every-constructor/W3C fixtures | overhead and coverage |
| small | DOID/HPO core-sized artifacts | Python fallback/CI |
| medium | MONDO, Uberon, GO basic/full | annotations, imports, RDF lists |
| large | GO-plus and a legally redistributable NCIt/large OBO artifact | multi-million triples/terms |
| composite | two OAEI Bio-ML source/target ontologies plus mappings | zero-copy coherence |
| synthetic | scaled declarations, restrictions, annotations, rules, imports | controlled asymptotics |
| adversarial | deep/symmetric/duplicate/fan-out/corrupt cases | bounded security cost |

Names do not imply mutable “latest” downloads. SNOMED CT or other restricted
ontologies may be a private optional lane only with documented license; release
gates cannot depend exclusively on unavailable data.

The model-schema-2 incident matrix adds pinned NCIt and FMA RDF/XML artifacts,
licensed SNOMED RDF/XML and Functional Syntax private lanes, and a
redistributable/generated Functional Syntax component-scaling corpus. Every
available input loads as one complete document under default limits; consumer
chunking is outside the result. Same-machine RSS ceilings, regression-only
counts, exact checksums, and the distinction between public release and private
incident closure are normative in `large-document-reliability.md`. Licensed
inputs cannot be the sole public release evidence.

Raw phase telemetry identifies structural component count/maximum size and
root-order interval activity, partition setup, refinement, candidate ordering,
key derivation, and freeze separately. It labels the XML-level SNOMED preflight
as an over-approximation rather than structural-model evidence. Counts/
signature and an independent alpha-equivalence migration oracle match a
raised-limit model-schema-1 baseline where one can complete.

Each required syntax has equivalent/representative inputs. RDF/XML and Turtle
receive primary scale coverage; OWL/XML/Functional receive complete correctness
and at least medium scale.

## 4. Scenarios

### 4.1 Parse and closure

- root with no imports;
- import diamond with repeated shared dependency;
- legal import cycles;
- local catalog closure and warm acquisition/document cache;
- annotation-heavy and blank-node/list-heavy RDF;
- source-map off/on; and
- Python, native single-thread, native bounded-parallel.

The native engine is exercised both through a direct Rust benchmark harness and
through the installed Python native wheel. The former compares engine design to
Horned-OWL without Python-wrapper noise; the latter proves the delivered facade
retains the same ontology without hidden conversion. The two lanes are never
pooled.

Report MB/s source, triples/s where known, axioms/s, expressions/s, imports/s,
time to first diagnostic/result, end-to-end time, peak/current RSS, tracked heap,
temporary bytes, allocations where available, and output size.

### 4.2 Indexes and queries

For each built-in index: build time/bytes, warm query latency/throughput,
canonical iteration, eviction/rebuild, and simultaneous distinct-index builds.
Query mixes reflect Exact labels/hierarchy, pyELK/HermiT compilation scans, and
projector restriction/property traversals.

### 4.3 Wire/cache

Canonical encode/write, full decode, validated mmap open, first/random/full
scan, concurrent readers, and cache hit/miss. Report file size, temporary copy
bytes, pages/RSS where measurable, digest time, and startup-to-first-query.

### 4.4 Overlay/composition

On a large base: 1/100/10k add/remove deltas, depth 1/8/32, lookup/iteration/
index patch, compact/materialize. Compose two large OAEI views plus 10/1k/100k
bridge axioms and vary bridge overlays across a repair batch.

Creation must be O(number of members + delta), excluding fingerprint work that
is reported/lazy where allowed. Measure incremental RSS before/after with bases
already resident and prove arena identity/reference reuse via instrumentation.

### 4.5 Consumer handoff

Instrument parser/resolver/encoder/object-construction counters while one
Exact-OM load feeds projector, pyELK/pyHermiT compiler, and evaluator. Handoff
must increment none of those counters and retain exact object identity. Report
core index reuse and private compiler allocations separately.

## 5. Methodology

- dedicated versioned reference machines plus informative CI smoke machines;
- fixed OS/CPU/RAM/storage, Python/Rust/compiler/dependency versions and power
  mode recorded with every result;
- process isolation and fresh-process cold trials; OS cache state stated, never
  implied;
- warm-up then enough iterations for stable confidence; large loads may use at
  least 5 measured runs, smaller at least 20;
- median, p90/p95, MAD/bootstrap confidence interval and raw samples retained;
- CPU time and wall time, peak RSS via platform tool, core tracked allocations,
  mmap/file size and temp disk; Python `tracemalloc` only as supplemental;
- no debugger/profiler in gate runs; separate profiles/flamegraphs retained;
- deterministic and safety/resource checks enabled as production defaults; and
- outputs/fingerprints compared before accepting timing samples.

Comparative jobs randomize implementation order within paired blocks. They pin
the exact pyowl-core wheel/revision, Rust allocator and thread ceiling,
Horned-OWL crate/revision/features, py-horned wrapper where used, OWLAPI jars,
JDK/GC/heap settings, corpus bytes, resolver map, and reference-machine key.
The pin ledger also contains a phase-by-phase inclusion table for every lane:
byte receipt, syntax/RDF parse, RDF-to-OWL mapping, interning, canonicalization,
freeze, each public/core fingerprint, provenance/diagnostics, required indexes,
common-adapter traversal/digests, publication, and post-timer equality checks.
Each row names `inside`, `outside`, or `not-applicable` with a rationale. A
timing report that lacks this table is not comparative evidence.
An unavailable comparator is `not-run`, never a pass. It blocks a claim against
that comparator and blocks the 1.0 comparative target where required, but does
not introduce that comparator into distributed runtime artifacts.

Benchmarks never fetch a network import inside measured parsing. Acquisition is
prepared/pinned or measured as a separate network scenario.

## 6. Baselines

Versioned `benchmarks/baselines/<machine>.json` includes raw metadata and
distributions. Initial evidence captures:

- current Exact-OM py-horned/RDF reparse path where runnable;
- direct Horned-OWL native loading and py-horned wrapper paths under pinned,
  reviewed versions;
- the complete Python backend;
- the direct native engine and installed native-wheel paths; and
- development-only OWLAPI loading in an isolated Java environment as a required
  comparative target, never a runtime/build dependency or user-install test.

Comparisons must use equivalent format/import/model/source-map semantics.
Claiming a speedup from dropping annotations/imports or returning partial
mapping is invalid.

## 7. Comparative native-loading contract

### 7.1 Equivalent readiness and correctness fence

The authoritative boundary definitions and the two Horned lanes are in
`native-ontology-redesign.md`. The gating `common-contract-ready` resident-byte
timer starts immediately before the implementation receives the bytes. It ends
only after a query-ready result plus the versioned common-contract ledger is
published: canonical constructor records, entity/signature records, document/
ontology/import identity, diagnostics/provenance, and the document, structural,
logical, and signature fingerprint preimages/digests. One complete native/bulk
traversal consumes every record. The traversal is a materialization fence: lazy
native work cannot escape the timer, but the benchmark MUST NOT construct a
second Python ontology merely to traverse it.

For pyowl-core, parsing, RDF mapping, interning, canonicalization, freeze,
required index construction, all four fingerprints, provenance, publication,
and the ledger traversal are inside. For Horned, engine loading plus every
independent-adapter mapping, canonical-order pass, provenance reconstruction,
fingerprint computation, and ledger traversal needed to reach identical output
are inside and separately phase-reported. `horned-model-ready` stops before
that adapter and is reported only as an explicitly asymmetric diagnostic lane;
it never supplies the denominator for an equivalence ratio.

Path/file loading is a separate lane. Acquisition, download, and manifest hash
verification occur before resident-byte timing. Import resolver I/O is reported
separately; closure CPU timings use the same prepared local resolver map.

After the timed sample, the harness only compares the already-produced ledger
bytes/counts/digests and validates samples. It performs no structural
normalization, fingerprint construction, or provenance recovery outside the
timer. A comparator that cannot retain or export an input required by the
common adapter makes that corpus ineligible for common-contract comparison;
the unsupported construct remains covered in pyowl-core's complete capability
lane and cannot be silently dropped.

### 7.2 Required modes and statistics

Two modes are reported independently:

- `fresh-process`: one load per new process; OS page-cache state is measured and
  labeled. A run is called `cold` only when the recorded machine procedure
  actually evicts or bypasses the relevant pages.
- `steady-process`: one persistent runtime, fixed equal predeclared warm-up,
  resident input bytes, a fresh ontology per repetition, and a cleanup barrier
  between repetitions.

Startup-to-ready and call-to-ready wall/CPU time are separate metrics. Peak RSS
uses an isolated process for fresh samples. Steady runs report absolute RSS and
incremental peak above the quiescent runtime, with mapped pages and temporary
bytes separately visible. Runtime heaps are not subtracted merely because one
comparator uses Python or a JVM.

Paired raw samples are retained. Each ratio is `pyowl-core native / comparator`.
Reports use paired bootstrap 95% confidence intervals for per-corpus medians
and the geometric mean across the required medium/large comparator corpus. Tiny
and small results remain informative and are governed by the existing AUTO
dispatch gate, but fixed startup cost does not determine large-load equivalence.

### 7.3 Minimum and stretch outcomes

The minimum retained-native performance gate compares the direct pyowl-core
Rust engine with Horned-OWL plus its timed independent common-contract adapter
in both fresh-process and steady-process resident-byte lanes:

| Metric | Aggregate upper 95% confidence bound | Per required large-corpus guardrail |
|---|---:|---:|
| query-ready wall time ratio | `<= 1.10` | median ratio `<= 1.25` |
| incremental peak RSS ratio | `<= 1.15` | median ratio `<= 1.25` |

The required common-profile set MUST contain at least one large RDF/XML
biomedical ontology and one annotation/list-heavy medium-or-larger ontology.
The gate cannot be declared from synthetic or Functional Syntax inputs alone.
Both aggregate bounds and every guardrail pass; a fast corpus cannot average
away a pathological regression.

Reports additionally show raw `horned-model-ready` time/RSS and the incremental
adapter cost. That raw ratio is labeled “pyowl-core full readiness versus raw
Horned model readiness,” not equivalence. The `<= 1.10` threshold is not
softened pre-emptively; WP14 may propose a separate reviewed threshold change
only with representative paired evidence and must preserve both lane labels.

The installed native-wheel lane compares the delivered pyowl-core call with the
pinned py-horned wrapper over the same Horned version plus the timed
common-contract adapter required for equivalent readiness.
It must meet the same `<= 1.10` aggregate/`<= 1.25` large-corpus wall-time
limits and `<= 1.15`/`<= 1.25` RSS limits. If the wrapper cannot expose an
equivalent result, the benchmark supplies only a bounded neutral traversal
adapter and records that limitation; it cannot omit structural work.

The installed native-wheel lane additionally MUST:

- publish the retained view with O(documents + bounded report metadata) Python
  objects before scalar access, as proven by allocation counters;
- complete encoded-view traversal without per-axiom Python callbacks; and
- add no more than 15% median call-to-ready overhead over the direct native
  engine on the aggregate required set, with the cause of remaining overhead
  phase-reported.

The stretch objective is to be the fastest implementation tested: upper-bound
geometric-mean time and RSS ratios below `1.00` against both Horned-OWL and
OWLAPI, with no required large scenario slower/larger than either. WP14 records
more aggressive optimization targets only after representative baselines are
available. Failure to beat OWLAPI remains a documented open performance target
if the Horned minimum passes; it cannot be described as an OWLAPI win.

OWLAPI executes only in an isolated development benchmark job. Horned-OWL and
OWLAPI are comparators, not runtime/build/test-suite dependencies, and their
licenses/artifacts do not enter release wheels merely because the benchmark is
required.

## 8. General release gates

After the first approved baseline, on reference hardware:

- no end-to-end required scenario median regression >10% or peak RSS >10%
  without reviewed evidence/intentional baseline update;
- no p95 regression >15% on query/mmap startup scenarios;
- native large parse/closure is at least 2x Python throughput or has a documented
  profile showing a different measured benefit sufficient to justify native;
- native is not slower than Python by >10% on small workloads after dispatch
  overhead (otherwise `AUTO` size-thresholds to Python before work);
- mmap warm startup is at most 25% of full parse/freeze wall time and does not
  eagerly allocate >10% of decoded structural heap (OS page-cache accounting
  reported separately);
- in-process `coerce_snapshot` performs zero ontology-sized allocations and
  p95 stays below a small fixed calibration threshold set per machine;
- one-axiom overlay and two-base/no-bridge composite creation allocate no more
  than `max(16 MiB, 0.5% resident base structural bytes)` and instrumentation
  proves base arenas are shared;
- shared-import diamonds parse each exact source digest once;
- wire bytes and all results match across performance variants; and
- limit/adversarial scenarios remain bounded and cancellable.

Absolute throughput targets are recorded only after baseline hardware/corpora
are pinned; inventing portable MB/s numbers in advance is not meaningful.

## 9. Optimization order

1. Remove repeated parsing, model conversion, flattening, and accidental copies.
2. Stream inputs and avoid DOM/full intermediate RDF duplication where feasible.
3. Intern strings/IRIs/terms and build only demanded indexes.
4. Use bulk core encoded views across consumer boundaries.
5. Improve algorithms/data layouts informed by profiles.
6. Retain the complete native arena behind lazy facades rather than decoding it
   into Python storage.
7. Align native, encoded-view, and canonical wire columns where profiles prove
   this removes transformations without coupling their version contracts.
8. Add bounded parallelism after single-thread algorithms/layouts are sound.
9. Consider specialized unsafe/vectorized code only under native policy.

Micro-optimizing Python constructors before eliminating a second complete parse
is the wrong priority. Every optimization includes workload evidence, memory/
correctness comparison, fallback impact, and rollback path.

## 10. Regression workflow

PR smoke gates use redistributable small/medium cached corpora and calibrated
synthetics. Nightly/release runs execute large/composite/native/platform lanes.
A regression report links commit, environment, raw results, fingerprints,
profile, suspected phase, and variance analysis.

Baseline updates require review and an explanation (intentional feature, fixed
measurement, toolchain shift, or accepted tradeoff). Averaging away a regression
or changing corpus/options is forbidden. Performance claims in README/docs must
name corpus/version/hardware/backend/options and link reproducible evidence.
