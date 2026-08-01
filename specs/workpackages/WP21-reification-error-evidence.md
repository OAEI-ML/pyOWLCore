# WP21 — reification error evidence

## Goal

Make missing-main-triple and malformed reification failures explainable from
bounded structural evidence while preserving strict rejection.

## Read first

`large-document-reliability.md`, `parsing-imports.md`, `contracts.md`,
`security.md`, `verification.md`, and the WP20 handoff.

## Depends on

WP20.

## Owned paths

The Python and native RDF reification paths reopened after WP20, their public
bridge diagnostics, focused reification/differential tests, and
`reports/workpackages/WP21.md`, exactly as listed in the manifest. Changes to
generic mapping-report fields return to WP20 rather than forking that contract.

## Deliverables

- Stable reification diagnostic codes and bounded detail keys for reification
  subject, annotated source/property/target, target kind, and main-triple
  presence.
- Total, retained-evidence, and suppressed counts bounded by
  `max_diagnostics`, permitting reconciliation without treating examples as an
  exhaustive parity oracle.
- Equal Python/native evidence for absent, ambiguous, cyclic, and incomplete
  axiom/annotation reifications.
- Negative fixtures distinguishing a malformed whole document from the same
  failure caused by an external split that omitted the main triple.
- Sanitization, truncation, deterministic ordering, and diagnostic-cap tests.

## Acceptance

- An absent main triple is never synthesized and the public error contains
  enough bounded evidence to identify the attempted assertion.
- Missing/ambiguous metadata is represented by a stable code and absent fields,
  not placeholder or guessed values.
- Forced Python/native tests compare contractual details and ignore prose.
- The pinned FMA whole-document orphan case is auditable; the historical
  107,588 chunk-caused removals cannot be cited as validated parity and vanish
  from the WP23 one-document path.
- No evidence field can bypass source excerpt, credential, IRI, diagnostic, or
  memory limits.

## Non-goals

No repair of invalid RDF exports, partial snapshot acceptance, parser
segmentation, or change to OWL reification semantics.
