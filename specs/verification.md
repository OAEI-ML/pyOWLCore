# Verification, conformance, and release evidence

## 1. Correctness precedence

1. W3C OWL/RDF Recommendations and applicable errata define language behavior.
2. This repository's explicit structural/import/wire contracts define choices
   left to an API.
3. Differential implementations are evidence, not authorities.
4. A pinned consumer quirk is implemented in its adapter, never by forking core
   semantic identity.

Any disagreement is captured in `tests/data/deviations.toml` with fixture,
references, expected core result, comparator versions, rationale, owner, and
review date. There is no undocumented “known failure” allowlist.

## 2. Test layers

```text
unit                 every constructor, invariant, option, diagnostic
generative           grammar/model values, permutations, metamorphic relations
format conformance   W3C syntax/mapping positive and negative cases
round trip           syntax families and canonical wire
imports/security     resolvers, graph identity, policies, hostile inputs
backend differential Python/native/independent schema implementation
implementation diff Horned-OWL/py-horned/RDF tools and dev OWLAPI where useful
consumer integration Exact-OM, pyELK, pyHermiT, projector, evaluator
packaging             installed wheels/sdist, Python/platform/artifact audits
performance           biomedical and adversarial gates
```

Tests assert model values, diagnostics/fingerprints/bytes where contractual,
provenance and absence of forbidden work—not only “no exception.”

## 3. Constructor coverage ledger

A generated ledger maps each OWL 2 structural grammar production to:

- public model constructor/tag;
- local/global validator rules;
- signature/reference visitor branches;
- all parser and required writer implementations;
- canonical and wire encoder/decoder tags;
- Python/native unit/generative fixture IDs; and
- consumer support/profile expectations.

CI fails on an unhandled constructor or stale generated ledger. `else: skip`
visitor logic is forbidden for required structures.

## 4. W3C and corpus provenance

WP01/WP09 assemble only legally usable fixtures. `tests/data/PROVENANCE.toml`
records origin URL, exact revision/date, hash, license/terms, transformations,
and redistribution status for every external case.

Required suites include OWL 2 conformance/test repository syntax/mapping cases,
RDF/XML/Turtle syntax tests as applicable, OWL/XML/Functional examples and
errata regressions. Harness selection is documented; skipping a case requires a
deviation entry, not a broad marker.

Large benchmark ontologies are manifests/downloads when redistribution is not
permitted. Tests verify hashes. No mutable URL or “latest” artifact is evidence.

Live-incident evidence follows the same rule: byte size or a truncated digest
does not identify an ontology. Restricted SNOMED data may close a private
incident lane but cannot be the sole public release gate; a redistributable or
generated corpus must exercise the same Functional Syntax component-scaling
property.

## 5. Generative and metamorphic tests

Bounded recursive strategies generate every primitive, expression, annotated
axiom, rule, document, import graph, delta, overlay, and composite, including
legal/illegal whole-ontology cases. Shrinking retains the relevant invariant.

Required metamorphic relations include:

- permuting unordered operands/axioms/import scheduling does not change model
  identity, fingerprints, canonical rendering/wire;
- ordered property chains and n-ary data property sequences do change identity
  when permuted;
- duplicate unordered operands collapse according to canonical parsing;
- prefix/base spelling, source order/layout, path, backend and Python hash seed
  do not affect structural content;
- language-tag case changes preserve canonical literal identity but source maps
  preserve lexical spelling;
- alpha-renaming blank labels preserves identity/fingerprints, while moving an
  anonymous individual to another document does not;
- permuting disconnected anonymous components preserves output; repeated
  isomorphic components retain multiplicity and distinct canonical occurrence
  slots; and canonical work is component-scoped without weakening global term
  bounds;
- parse → write → parse preserves structure; RDF results are graph-isomorphic;
- equal effective overlay histories have equal semantic fingerprints;
- overlay/composite query results equal independent materialization; and
- encode → decode → encode is byte-identical.

Invalid generators target every arity/type/profile/resource/wire constraint and
assert a stable category/code without requiring exact prose.

## 6. Differential strategy

### 6.1 Python versus Rust

This is a release requirement for all supported features. Compare canonical
values, reports/diagnostic codes, fingerprints, writer bytes where canonical,
wire bytes, indexes, limits/cancellation, and consumer outputs. Randomized tests
run forced backend and reject native fallback.

Configured-limit differential fixtures compare typed `limit`, `observed`,
`allowed`, and immutable details without inspecting messages. Strict RDF
mapping fixtures compare the attached counts/rule IDs/bounded triple evidence,
including object kind, from the first failing parse. Missing-main-triple
reification fixtures compare bounded diagnostic detail keys. Anonymous model
schema 2 fixtures include disconnected non-isomorphic components, repeated
isomorphic components, one oversized component, and blank-label/root/component
permutations against the independent canonical implementation.

The four-input incident lane records post-mapping structural component count,
maximum labels/arcs/roots/span, and maximum open root intervals for NCIt, FMA,
and both SNOMED serializations. Any XML-level preflight is labeled as an
over-approximation. Native limit tests drive both confirmed
`max_canonical_work` enforcement paths and compare structured fields despite
different messages. FMA reification tests retain total/evidence/suppressed
counts and never infer annotation parity from a complete composite flag.

Retained-native tests additionally compare lazy scalar materialization and the
complete `EncodedStructuralView` against independent Python traversal for every
constructor. Allocation/object counters assert publication is not proportional
to terms/axioms, facade caches remain bounded, and direct/mapped owners survive
iteration, close, GC, fork, and concurrent reads without dangling buffers.

Native capability advertisement is an installed-path integration gate, not a
unit-test conclusion. Before a format or encoded-view schema appears in
`CoreCapabilities`, a forced-native test must start from real syntax bytes (or
a deliberately corrupt descriptor), pass through acquisition, parse/RDF
mapping, canonical freeze, snapshot publication, scalar facade, encoded view,
wire/mmap where applicable, and at least one public consumer operation. It runs
positive, interacting-feature, negative, limit, cancellation, and malformed
cases and asserts Python/native values and public error categories exactly.
The capability bit is absent until that whole matrix passes; fallback is
disabled during the test.

The retained regression corpus includes cross-layer shapes that isolated unit
suites historically missed. It includes the OAEI/pyHermiT incoherent ontology
with an unsatisfiable class below a multi-level satisfiable superclass chain,
plus nested RDF lists/annotations/import provenance and segmented composite
variants. Core does not assert reasoner semantics, but it must hand the exact
same structural view to forced Python/native consumer runs so any downstream
parity mismatch blocks the corresponding consumer/workspace claim.

### 6.2 Independent wire/canonical implementation

A deliberately small independent Python schema reader/encoder under test tools
does not import production canonical/wire code. It validates goldens and detects
a shared production bug. It is not installed as runtime API.

### 6.3 External implementations

Pinned Horned-OWL/py-horned-owl and standards-compliant RDF parsers provide
structural/RDF graph comparisons. Development-only OWLAPI tools may render/
parse/compare in an isolated Java-enabled job. Java is never imported,
downloaded, invoked, or required by package tests, user installation, release
artifacts, or normal CI lanes.

External output is normalized only by documented syntax/blank-node rules. A
disagreement is investigated against W3C; majority vote does not decide.

For comparative performance, the external adapter must also emit the exact
inventory/digest fence in `performance.md`; an unsupported or unequal result is
ineligible for timing rather than normalized into apparent parity.

## 7. Import and lifecycle verification

The import matrix crosses every policy with missing/resolved/conflicting
documents, local/catalog/composite/HTTP resolvers, offline/cache states,
diamonds, legal ontology cycles, alias/redirect cycles, version/ontology IDs,
parallel schedules, cancellation and limits.

Lifecycle tests cover nonseekable/short-reading streams, caller-owned stream
closure, mmap close/dependent views/fork, exception cleanup, concurrent index
creation, cache publication crashes/locks, overlays/composites retaining bases,
and Python GC. File descriptor/mapping/temp/thread leaks are measured.

## 8. Hostile and fuzz testing

- Python parser/model/wire property fuzzers in ordinary CI;
- native libFuzzer/AFL targets for each syntax, RDF mapping, canonicalizer, and
  wire sections, with sanitizer jobs;
- compact fixture truncated at every byte and systematic bit/field corruptions;
- saved minimized regression corpus with provenance and stable expected result;
- time/memory/allocation failure injection and cancellation at phase boundaries;
- XML entity/DTD, RDF list, Unicode, path, SSRF, compression, diagnostic and
  cache attacks from `security.md`; and
- no panic, abort, segfault, unbounded allocation/work, data race or partial
  published artifact.

Fuzz success is measured by constructor/rule/error coverage and retained corpus,
not hours alone.

## 9. Consumer integration matrix

For Exact-OM, pyELK, pyHermiT, projector, and OAEI evaluator, tests run with:

- path standalone input;
- an already loaded snapshot;
- provider-wrapped snapshot;
- one/many-delta overlay;
- source+target+bridges composite where applicable;
- decoded and mmap wire snapshot;
- pure core and native core; and
- Python 3.10 plus current supported versions.

Instrumentation asserts exact in-process identity, zero parser/resolver/wire
calls on view input, shared core index reuse, no private core import, and no
Java process/module/artifact. Consumer private IR/results match path baselines.
Profile-incompatible inputs produce complete diagnostics, never silent axiom
drops or incomplete reasoning.

Native performance lanes negotiate `EncodedStructuralView` and additionally
assert zero scalar axiom materializations/per-axiom Python callbacks during
consumer compilation. Overlay/composite runs prove the encoded representation
retains base owners and transfers only delta/member postings unless explicit
materialization was requested.

The matrix distinguishes core capability advertisement from external consumer
optimization. Core advertisement requires the independent decoder and the
installed-path integration fence in section 6.1. Specific successor companion
artifacts may be `not-run` for `core_release_eligible`, but they must all pass
before `workspace_optimization_complete` or multi-consumer native-performance
claims. Exact and OAEI are compatibility consumers; only pyELK, pyHermiT, and
the projector own encoded compilers.

The language-tag compatibility fixture proves canonical shared identity,
pyHermiT canonical consumption, and pyELK's isolated legacy key behavior with/
without source map.

## 10. Packaging verification

Test the built artifact, not only the source tree:

- pure wheel install/full semantic suite with compilers and Java absent;
- sdist pure mode offline build and test;
- each native wheel install, forced-native full/parity suite and dynamic audit;
- local index with all wheels verifies resolver choice on each interpreter;
- Python 3.10 through newest supported, CPython/PyPy policy, platform matrix;
- metadata/import/type checking/docs examples/CLI if added;
- unpacked artifact/SBOM/license/NOTICE/provenance/Java scans; and
- import side-effect/network/cache/plugin tests.

Before first publication, reserve/confirm PyPI `pyowl-core` and replace metadata
placeholders. Name ownership is a release gate, not a best-effort checklist.

## 11. Coverage and quality gates

- 100% constructor/tag/visitor/parser-writer/wire dispatch coverage;
- branch coverage target >=95% for pure Python core, with exceptions reviewed;
- no untyped public API and strict mypy/pyright-compatible stubs/examples;
- formatting/lint, docs links, schema generation clean diff;
- no expected-failure marker without deviation ID/owner/removal condition;
- no network-dependent ordinary unit test; and
- deterministic tests under multiple hash seeds/locales/timezones/thread counts.

Coverage percentage cannot substitute for W3C production and mutation/error
coverage. Mutation testing targets validators, bounds checks, canonical sorting,
import policy, and wire references.

## 12. Performance correctness

Every benchmark validates output fingerprints/counts/index samples before
recording a timing. Native/parallel/cached/mmap variants compare with Python
cold structure. Memory gates include retained and peak allocations. A speedup
that skips unsupported triples, source annotations, imports, validation, or
digest checks is a correctness failure.

Horned/OWLAPI ratios are reported only after exact comparator output equality
and the common-readiness materialization fence. The direct native engine and
installed Python wheel are distinct lanes; wrapper conversion cannot be hidden
outside the delivered-path timer.

For Horned, the common-contract adapter's canonical ledger, all four
fingerprints, provenance/diagnostics reconstruction, and traversal are timed.
Only comparison of already-produced outputs is post-timer. Raw Horned model
readiness is retained as an explicitly asymmetric diagnostic and never used as
the equivalence denominator.

Raw results/environment/corpus manifests are retained. Regression thresholds
and methodology are in `performance.md`.

## 13. Release evidence report

Each release generates `reports/release/<version>/` containing:

- schema/API/version/constructor coverage ledgers;
- W3C/deviation/differential summaries;
- Python/native/platform/consumer matrices;
- fuzz/sanitizer/security/resource reports;
- benchmark raw/summary comparisons;
- wheel/sdist install/audit/Java/SBOM/license/name evidence;
- corpus provenance and exact toolchain/lock hashes; and
- known limitations with owner/target version.

## 14. Release blockers

Any of these blocks release:

- structurally dropped/misidentified construct or unresolved unexplained
  external differential;
- Python/native semantic, fingerprint, canonical or wire mismatch;
- non-deterministic result/cache identity;
- ordinary import cycle rejection or hidden unresolved import;
- unsafe/unbounded hostile-input behavior, panic/abort/leak/data race;
- compiler required for the complete fallback;
- consumer reparsing/copying on an in-process view;
- bundled/runtime/build Java dependency or Java network bootstrap;
- missing/inaccurate third-party license obligations;
- unreserved/uncontrolled PyPI name or placeholder release metadata; or
- failed performance gates without an explicitly approved spec/baseline change.
