# WP19 — structured resource-limit errors and canonical telemetry

## Goal

Make every configured-limit failure classifiable without message parsing and
add bounded component/work telemetry needed to validate the large-document
canonicalization change.

## Read first

`large-document-reliability.md`, `contracts.md`, `security.md`,
`native-backend.md`, `verification.md`, and the WP18 handoff.

## Depends on

WP18.

## Owned paths

The public exception/diagnostic contract, Python/native limit and error frames,
their single conversion boundary, anonymous-work observation hooks, focused
limit differential tests, and `reports/workpackages/WP19.md`, exactly as listed
in the manifest. The anonymous hooks are observational only; WP23 owns the
subsequent algorithm and schema change.

## Deliverables

- A frozen Python `ResourceLimitError` contract with required `limit`,
  `observed`, `allowed`, and immutable bounded `details` for configured limits.
- A typed native error payload carrying the same fields through FFI, with a
  census eliminating message-derived limit classification.
- A regression for the confirmed initial-parse and cleaned-reification-retry
  enforcement paths, which currently emit different messages for
  `max_canonical_work`, proving their typed fields are identical.
- Stable canonicalization detail keys for component count, largest component,
  refinement rounds, and charged work term.
- Python-first unit/property tests followed by forced-native differential tests
  covering parser, canonicalizer, wire, index, import, deadline, and memory
  limits reachable through public operations.
- A phase/work telemetry report for the pinned available incident inputs; an
  unavailable licensed corpus is recorded `not-run`, not inferred.

## Acceptance

- Every configured-limit fixture exposes non-null typed values, and
  `as_diagnostic()` preserves the safe detail mapping.
- Python/native equivalent failures compare equal on contractual fields;
  message wording can change without breaking a test or consumer classifier.
- Equivalent failures compare equal across native call sites and reparses after
  caller modification, not merely between backend names.
- Native panics, protocol faults, and actual allocator failures retain their
  distinct public categories.
- Telemetry is bounded by diagnostic/resource policy and does not include blank
  labels, source excerpts, or ontology-sized data.

## Non-goals

No component partitioning, limit-default increase, RDF mapping behavior change,
or package/model version bump.
