# WP04 — overlays, zero-copy composition, and fingerprints

## Goal

Implement the common `OntologyView` protocol, persistent repair deltas/overlays,
zero-copy source+target composition, and independently verified canonical
fingerprints.

## Read first

`snapshots-overlays.md`, fingerprint sections of `model.md`, `contracts.md`,
`architecture.md`, and WP03 handoff.

## Depends on

WP03.

## Owned paths

Delta/overlay/composite/fingerprint files and tests from the manifest. Do not
build general indexes or consumer reasoner/projector state.

## Deliverables

- Runtime-checkable read-only `OntologyView` implemented by sibling concrete
  snapshot, overlay and composite types.
- `OntologyDelta` strict/idempotent validation, base fingerprint binding,
  persistent overlay layering, explicit compact/materialize.
- `compose_views` with member roles/provenance, anonymous-scope preservation,
  canonical deduplicated iteration, and no base arena copy.
- Structural/logical/signature/document fingerprint domain encoders plus an
  independent test implementation and incremental/full equivalence.
- Identity-preserving coercion for all views/providers and lifecycle retention.

## Acceptance

- `coerce_snapshot(x) is x` for snapshot/overlay/composite/provider result;
- randomized edit histories/compositions equal independent materialization and
  canonical full fingerprints;
- million-axiom base + one edit and two-base composition meet no-copy allocation
  assertions/instrumented arena identity;
- anonymous individuals stay apart and duplicate origins are retained;
- strict/idempotent/conflict/depth/close/concurrency cases pass; and
- WP05/WP06/consumers receive stable view/fingerprint contracts.

