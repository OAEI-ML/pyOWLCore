# WP11 — consumer adapters and zero-reparse conformance

## Goal

Finalize adapter/provider/bulk contracts and prove Exact-OM, pyELK, pyHermiT,
projector, and OAEI evaluation can share views in-process or via wire without
model/parser duplication.

## Read first

`adapters.md`, `contracts.md`, `architecture.md`, `snapshots-overlays.md`, each
consumer's migration spec, and WP05/WP06/WP08/WP09 handoffs.

## Depends on

WP05, WP06, WP08 and WP09.

## Owned paths

Only core adapter protocol/conformance kit/integration fixtures/reports in the
manifest. Actual consumer edits occur in their repositories under their specs;
do not import consumers from the core runtime package.

## Deliverables

- `CoreCapabilities`, adapter negotiation and stable
  `SnapshotProvider`/`OntologyView` conformance kit.
- Instrumented integration fixtures for path/snapshot/provider/overlay/
  composite/wire/mmap and Python/native core.
- Consumer migration coordination: exact type/export/version/fingerprint/cache
  names, unsupported-feature rules and deprecation boundaries.
- OAEI zero-copy source+target+bridge composition and batched overlay trial.
- Language-tag fixture proving one canonical core identity and isolated pyELK
  legacy compatibility key; pyHermiT consumes canonical form.

## Acceptance

- all consumers pass identity and zero parser/resolver/wire counter assertions;
- no consumer uses private core modules, an independent OWL model/parser, Java,
  path handoff, or pickle for the new path;
- unsupported profile/feature reports are exhaustive, not silent drops;
- standalone results match in-process and wire results;
- core runtime imports no consumer and plugin discovery has no side effects;
- cache keys bind correct core/consumer schemas and fingerprints; and
- integration report lists exact tested consumer commits/API ranges.

