# WP08 — native parsers, canonicalization, and indexes

## Goal

Accelerate complete operations in Rust with strict Python semantic parity and
coarse buffers, including required formats and measured hot indexes.

## Read first

`native-backend.md`, `parsing-imports.md`, `indexes-views.md`, `performance.md`,
`verification.md`, and WP02/WP05/WP07 handoffs.

## Depends on

WP02, WP05 and WP07.

## Owned paths

Native source/parser/mapping/canonical/index/session and native differential
tests listed in the manifest. Python reference logic is not rewritten here.

## Deliverables

- Native required format lex/parse/RDF mapping/document freeze one feature at a
  time, with complete capability gating.
- Native anonymous canonicalization/fingerprints and selected common index/
  overlay/composite posting operations proven by profiles.
- Coarse operation buffers, bounded deterministic parallelism, GIL/event/error
  integration and tracked memory.
- Cross-backend generated/conformance/hostile/roundtrip/index suites and
  profiles showing eliminated bottlenecks.

## Acceptance

- every advertised native operation passes full Python parity; no Python
  callbacks for missing semantic handlers and no halfway fallback;
- canonical/writer/wire bytes and diagnostics where contractual match;
- format fuzz/sanitizer/cancel/memory/panic/thread tests pass;
- unimplemented capability chooses Python before work;
- large benchmarks show justified native benefit without small-input regression;
- no public Rust/Horned type escapes and no license/Java policy violation.

