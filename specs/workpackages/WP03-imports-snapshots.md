# WP03 — import resolvers and immutable snapshots

## Goal

Build deterministic, secure import closure resolution and a concrete immutable
`OntologySnapshot` without reparsing accepted documents.

## Read first

`parsing-imports.md` sections 6–9, `snapshots-overlays.md` sections 1–3,
`contracts.md`, `security.md`, and WP02 handoff.

## Depends on

WP02.

## Owned paths

Resolver/import/snapshot and main loading facade paths in the manifest. Do not
implement overlays/composites/indexes/wire.

## Deliverables

- Mapping, catalog, directory, composite and opt-in HTTP resolver protocols/
  implementations with explicit security/resource/offline behavior.
- All four import policies, deterministic concurrent closure traversal,
  documents/edges/outcome manifest and origin union.
- Legal cycles/diamonds, alias deduplication, ontology/version/source conflicts,
  integrity and acquisition/document caches.
- `OntologySnapshot`, complete/root/document iteration/signatures baseline,
  manifest-aware structural fingerprint inputs and load report.
- `parse_document`, `load_snapshot`, `coerce_snapshot` facade behavior for
  documents/snapshots/providers available at this stage.

## Acceptance

- policy × resolver × outcome matrix including offline/HTTP security;
- ordinary ontology import cycles accepted/visited once; resolver redirect/
  alias cycles fail distinctly;
- concurrent traversal has identical manifest/fingerprint/diagnostics;
- accepted `OntologyDocument` is not reparsed and snapshot coercion preserves
  identity;
- conflict/integrity/limit/cancel/cache-crash cases publish no partial snapshot;
- root/document/closure anonymous scoping and origins are correct; and
- WP04 receives stable snapshot/manifest interfaces.

