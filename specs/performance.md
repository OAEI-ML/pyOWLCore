# Performance and biomedical benchmark contract

Correctness, determinism, safety, and the complete Python fallback are fixed
constraints. Optimization follows measurement and targets end-to-end retained
work, not a parser microbenchmark that then rebuilds Python objects repeatedly.

## 1. Performance objectives

1. Parse/canonicalize each physical ontology document at most once per snapshot.
2. Hand the same in-process `OntologyView` identity to every consumer with no
   ontology-sized allocation.
3. Keep common strings/IRIs/axioms/indexes shared and reasoner IR private.
4. Make native parsing/indexing/wire throughput competitive on multi-million-
   axiom biomedical ontologies while preserving a usable Python fallback.
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

Benchmarks never fetch a network import inside measured parsing. Acquisition is
prepared/pinned or measured as a separate network scenario.

## 6. Baselines

Versioned `benchmarks/baselines/<machine>.json` includes raw metadata and
distributions. Initial evidence captures:

- current Exact-OM py-horned/RDF reparse path where runnable;
- direct py-horned-owl/Horned-OWL candidate path under reviewed versions;
- the complete Python backend;
- the native backend; and
- where useful, development-only OWLAPI parsing in an isolated Java environment
  as context, never a runtime/build dependency or required user benchmark.

Comparisons must use equivalent format/import/model/source-map semantics.
Claiming a speedup from dropping annotations/imports or returning partial
mapping is invalid.

## 7. Release gates

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

## 8. Optimization order

1. Remove repeated parsing, model conversion, flattening, and accidental copies.
2. Stream inputs and avoid DOM/full intermediate RDF duplication where feasible.
3. Intern strings/IRIs/terms and build only demanded indexes.
4. Use bulk core encoded views across consumer boundaries.
5. Improve algorithms/data layouts informed by profiles.
6. Add Rust coarse operations and bounded parallelism.
7. Consider specialized unsafe/vectorized code only under native policy.

Micro-optimizing Python constructors before eliminating a second complete parse
is the wrong priority. Every optimization includes workload evidence, memory/
correctness comparison, fallback impact, and rollback path.

## 9. Regression workflow

PR smoke gates use redistributable small/medium cached corpora and calibrated
synthetics. Nightly/release runs execute large/composite/native/platform lanes.
A regression report links commit, environment, raw results, fingerprints,
profile, suspected phase, and variance analysis.

Baseline updates require review and an explanation (intentional feature, fixed
measurement, toolchain shift, or accepted tradeoff). Averaging away a regression
or changing corpus/options is forbidden. Performance claims in README/docs must
name corpus/version/hardware/backend/options and link reproducible evidence.

