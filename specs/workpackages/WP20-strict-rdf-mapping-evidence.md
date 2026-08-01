# WP20 — strict RDF mapping evidence

## Goal

Attach a complete bounded `RDFMappingReport` to the first strict mapping
failure, with identical Python/native evidence and no diagnostic reparse.

## Read first

`large-document-reliability.md`, `contracts.md`, `parsing-imports.md`,
`native-backend.md`, `security.md`, `verification.md`, and the WP19 handoff.

## Depends on

WP19.

## Owned paths

RDF report/evidence values, the Python RDF mapper, native RDF mapping ledger and
public bridge/stub, focused format/differential tests, and
`reports/workpackages/WP20.md`, exactly as listed in the manifest. WP21 receives
an explicit sequential handoff over the RDF mapper files.

## Deliverables

- `RDFTripleEvidence.object_kind` with the stable IRI/blank/literal vocabulary
  and bounded/redacted values.
- `UnsupportedSyntaxError.rdf_mapping_report`, populated before strict
  `RDF_MAPPING_INCOMPLETE` is raised.
- Native mapping-ledger construction that retains counts, rule IDs,
  diagnostics, and the first deterministic `max_diagnostics` examples without
  a second graph traversal proportional to the input.
- Public normalization of backend-specific error codes.
- Python-first and forced-native tests, including a pinned redistributable DOID
  fixture that recovers all bounded predicate/subject evidence in one parse.

## Acceptance

- Strict rejection remains the default and never guesses an ambiguous
  undeclared predicate.
- The attached report has valid `0 <= consumed <= total`, is nonconformant, and
  matches the partial diagnostic report for the same graph up to the configured
  evidence bound.
- Python/native report fields and deterministic example order agree.
- Instrumentation proves consumers need no second parse to classify the DOID
  mapping failure.

## Non-goals

No automatic declaration repair, partial snapshot loading, reification evidence
schema, or anonymous canonicalization change.
