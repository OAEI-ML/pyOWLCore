# WP22 — supported partial RDF diagnostic mode

## Goal

Resolve the existing half-public partial RDF mapping switch by exposing it only
as a safe one-document diagnostic option in API line `(0, 2)`.

## Read first

`large-document-reliability.md`, `contracts.md`, `parsing-imports.md`,
`architecture.md`, `security.md`, `verification.md`, and the WP21 handoff.

## Depends on

WP21.

## Owned paths

`LoadOptions`, curated parsing API/routing, RDF format/backend option plumbing,
public exports/stubs and API audit, focused option/negative/docs tests, and
`reports/workpackages/WP22.md`, exactly as listed in the manifest. WP23 owns the
single coordinated package/API/model release ledger and final version values;
WP22 supplies its frozen API handoff.

## Deliverables

- `LoadOptions.allow_partial_rdf_mapping: bool = False` on the curated API.
- One-document RDF/XML/Turtle behavior returning a nonconformant document with
  the WP20 report and explicit dropped-statement counts.
- Stable option-conflict failures for non-RDF formats, snapshot/coercion
  acquisition, and already parsed nonconformant documents.
- Public documentation and type/API audit updates that label the mode
  diagnostic-only.
- Tests proving defaults and every reasoner/snapshot/cache/wire route remain
  fail-closed.

## Acceptance

- The option is either accepted under the exact diagnostic contract or rejected
  before input consumption; it is never ignored.
- A partial document cannot become an `OntologySnapshot`, conformant cache,
  wire snapshot, or reasoner input through a public route.
- Strict and partial first-pass reports agree on counts and bounded examples.
- Python/native supported RDF paths behave identically.

## Non-goals

No editor recovery document, best-effort snapshot, declaration inference,
consumer workaround, or relaxation of strict defaults.
