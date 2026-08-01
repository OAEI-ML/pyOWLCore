# WP23 — component-scoped anonymous canonicalization v2

## Goal

Replace document-global anonymous canonicalization with the
multiplicity-preserving component scheme, complete the model/API/encoded-schema
transition, and close the one-pass large-biomedical-document incident.

## Read first

All focused specifications, especially `large-document-reliability.md`,
`model.md`, `security.md`, `performance.md`, `verification.md`,
`wire-format.md`, and `native-ontology-redesign.md`, plus WP19-WP21 handoffs and
the WP22 API handoff when available.

## Depends on

WP21. WP22 may execute in parallel; its frozen API handoff is required before
the final version ledger/release commit, not before component implementation.

## Owned paths

Python/native anonymous graph/freeze/snapshot rescoping, schema/version
generators and ledgers, encoded structural schema 2 and its native/public
producers, independent canonical/schema references, anonymous/differential/
wire/encoded-view tests, incident benchmark manifests/harness/reports, release
metadata/changelog/migration, and `reports/workpackages/WP23.md`, exactly as
listed in the manifest. WP19 telemetry hooks and WP22 public option semantics
are frozen inputs.

## Deliverables

- Bounded component partitioning with document-global term/memory accounting
  and per-component `max_canonical_work`.
- Complete canonical component graph bytes, sorted multiplicity manifest,
  repeated-isomorphic-component occurrence slots, v2 scope/key/color/graph/
  rescope domains, and cached phase-one orders.
- A small independent model-schema-2 implementation and adversarial/property
  goldens proving label/root/component/hash-seed/backend invariance without
  collapsing distinct anonymous individuals.
- Equivalent Python and retained-native implementations plus component phase/
  work/allocation telemetry.
- Model schema 2 and encoded structural schema 2 generation/registration; model
  schema 1 and encoded schema 1 are never reinterpreted.
- A release ledger for package `0.2.0`, API `(0, 2)`, model 2, wire `(1, 2)`,
  adapter 1, and encoded schema 2; all metadata, docs, compatibility/migration,
  and consumer capability fixtures agree.
- Pinned/licensed NCIt evidence and a redistributable/generated Functional
  component-scaling gate; licensed SNOMED evidence is private/optional.

## Acceptance

- Distinct repeated isomorphic components survive with distinct keys; arbitrary
  renaming/permutation produces byte-identical outputs; true duplicate roots
  still obey normal canonical-set semantics.
- Charged work scales as the sum of component work at fixed component size, and
  one oversized connected component returns WP19's structured limit error.
- Python/native canonical values, scopes, keys, fingerprints, wire behavior,
  encoded schema 2, diagnostics, and rescoping are equal under independent
  verification.
- The pinned NCIt artifact loads in one pass with default limits and lower
  same-machine peak RSS than the documented chunked incident, with counts,
  signature, and alpha-equivalence checked against a raised-limit baseline.
- Public Functional Syntax scaling evidence is release-gating; unavailable
  licensed SNOMED is `not-run` and blocks only the private incident claim.
- The package installs as `0.2.0`; stale model/schema caches fail closed or are
  regenerated, and all supported consumers negotiate the new values.
- WP23 does not close or make the final release/version commit until WP22's
  frozen API handoff is incorporated and its conflict-route tests pass.

## Non-goals

No segmented/streaming document API, consumer chunker, strict-mapping
relaxation, default limit increase, reasoner behavior, or unpinned performance
claim.
