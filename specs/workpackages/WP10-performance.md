# WP10 — biomedical performance and regression gates

## Goal

Build reproducible phase/memory benchmarks, capture baselines, remove repeated
work/copies, and enforce performance gates on real large biomedical ontologies.

## Read first

`performance.md`, `security.md`, `packaging.md`, and WP05/WP06/WP08 handoffs.

## Depends on

WP05, WP06 and WP08.

## Owned paths

Benchmarks, benchmark tooling and performance reports in the manifest. Corpus
bytes require provenance and may be manifests/downloads rather than committed.

## Deliverables

- Pinned licensed `corpora.toml` covering tiny through large, imports,
  annotation/list-heavy, OAEI composite, synthetic and adversarial families.
- Phase-separated cold/warm/cache/mmap Python/native parse, index, wire,
  overlay/composite and consumer handoff harness with output validation.
- Reference machine metadata/raw statistics/baselines and comparative evidence
  against current Exact/py-horned paths where legally/runnably available.
- Allocation/arena/parser-call instrumentation proving parse-once and no-copy.
- Profiles and measured optimizations in owning WPs; regression report workflow.

## Acceptance

- methodology/repetitions/cache state/environment/output fingerprints recorded;
- release thresholds in `performance.md` calibrated and passing;
- handoff invokes zero parse/resolve/wire and preserves identity;
- overlay/composition/mmap meet incremental memory gates;
- native benefit justifies artifact while Python fallback remains viable;
- security/determinism/validation settings are not disabled for speed; and
- raw results/corpus hashes can be reproduced on a clean reference machine.

