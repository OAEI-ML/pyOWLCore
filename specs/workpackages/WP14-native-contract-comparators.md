# WP14 — retained-native contract and comparator baseline

## Goal

Turn the native ontology redesign into measurable, implementation-ready
boundaries and capture correctness-qualified baselines before changing storage.

## Read first

`native-ontology-redesign.md`, `performance.md`, `native-backend.md`,
`contracts.md`, `indexes-views.md`, `wire-format.md`, and the WP09/WP10/WP13
handoffs.

## Depends on

WP09, WP10 and WP13.

## Owned paths

Comparator manifests/orchestration, redesign baseline reports, benchmark
contract tests, and this redesign specification as listed in the manifest.
Comparator binaries/environments are development-only and excluded from every
package artifact.

## Deliverables

- Exact timed-boundary and output-inventory adapters for direct pyowl-core Rust,
  installed native wheel, current Python backend, Horned-OWL, py-horned, and an
  isolated OWLAPI runner.
- Pin ledger containing source/artifact revisions and hashes, features,
  allocators, JDK/GC/heap settings, thread ceilings, corpora, resolver maps, and
  reference-machine identity.
- A normative per-lane timing fence table marking byte receipt, parse/mapping,
  interning, canonicalization, freeze, each fingerprint, provenance,
  diagnostics, indexes, comparator-adapter traversal/digests, publication, and
  equality assertion as inside/outside/not-applicable. The table distinguishes
  raw `horned-model-ready` from gating `common-contract-ready`.
- An independent Horned common-contract adapter that constructs the same
  canonical structural ledger, four fingerprint preimages/digests, identity,
  imports, diagnostics, and bounded provenance. Its complete cost is inside the
  gating Horned timer and phase-separated from raw Horned engine loading.
- Fresh-process and steady-process raw baseline samples with wall/CPU time,
  peak/incremental RSS, temporary bytes, object counts, result inventories, and
  profiles for every current pyowl-core loading phase.
- A written storage/API decision covering private retained handles, ownership,
  lazy scalar materialization, encoded structural schema direction, mmap, and
  backend capability names.
- Profiles locating the current NCIT/DOID and representative medium/large
  costs; no optimization is accepted merely from a parser microbenchmark.

## Acceptance

- All comparator results use identical pinned bytes/options and pass the exact
  structural inventory/digest fence before timing ratios are calculated.
- Only `common-contract-ready` may be described as Horned equivalence or used
  for the `<= 1.10` aggregate gate. Raw Horned model readiness is an explicitly
  asymmetric diagnostic comparison. Post-timer work is limited to equality/
  sample validation and performs no canonicalization or fingerprint work.
- Direct-engine and delivered-Python lanes, resident-byte and file lanes, and
  fresh/steady modes are reported separately with raw samples.
- Unavailable or semantically ineligible comparators are `not-run`/ineligible,
  never treated as a pass or silently reduced feature set.
- Current retained-materialization/copy counters and phase profiles are
  reproducible on the approved reference machine.
- The version decision for the later encoded-view API is recorded without
  falsely advertising an unimplemented runtime capability.
- Any proposal to alter the aggregate/per-corpus Horned thresholds is a
  separate reviewed contract amendment supported by representative paired raw
  samples; WP14 does not silently soften the gate when establishing a baseline.
- No Horned or Java dependency enters pyowl-core runtime, build, ordinary test,
  sdist, or wheel dependency graphs.
