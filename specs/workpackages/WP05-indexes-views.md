# WP05 — reusable structural indexes and views

## Goal

Implement lazy immutable syntax-only indexes shared by consumers, including
delta/composite-aware merging without embedding inference or private IR.

## Read first

`indexes-views.md`, `architecture.md`, `model.md`, `performance.md`, and WP04
handoff.

## Depends on

WP04.

## Owned paths

`src/pyowl_core/index/` and listed index/cache tests. Consumer algorithms and
reasoner/projector indexes are forbidden.

## Deliverables

- Signature, axiom type, entity reference, declaration, annotation assertion,
  asserted class/property hierarchy, domain/range and expression occurrence
  views exactly as specified.
- Typed immutable options, once-cache, accounting/eviction, cancellation,
  canonical/bounded iteration and reports.
- Overlay posting patches and composite k-way dedup/origin merges.
- Complete generated constructor walkers and provisional core `EncodedView`
  only if a benchmark proves its design/lifetime/fallback value.

## Acceptance

- all results match independent scans/materialized views on generated corpora;
- exhaustive constructor/reference/role and root/document/closure option tests;
- hierarchy tests prove no inference/transitive reduction/chain flattening;
- equal requests share identity, cancellation/failure publishes no partial
  cache, concurrent builds/eviction are safe;
- small delta/composition avoids base-sized index allocation;
- memory/time accounting and pure-Python biomedical benchmark evidence; and
- imports/static inspection show no consumer-private IR.

