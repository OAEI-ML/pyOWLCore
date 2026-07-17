# WP01 — complete structural model and canonical schema

## Goal

Implement every OWL 2 structural value with one backend-independent identity,
canonical encoding, exhaustive visitors, factories, and local/global structural
validation foundations.

## Read first

`model.md`, W3C OWL 2 Structural Specification/errata in `references.md`,
`contracts.md`, `security.md`, and WP00 handoff.

## Depends on

WP00.

## Owned paths

`src/pyowl_core/model/`, `src/pyowl_core/extensions/`,
`schemas/model-v1.toml`, and model tests. This work package also owns the
coordinated WP01 additions to the root public exports/snapshot and model tag
generator. Do not add format parsers, snapshot/import behavior, reasoner IR,
or consumer quirks.

## Deliverables

- Immutable IRI, typed entities/punning, literal/language identity, scoped
  anonymous individuals, annotations and every expression/axiom/rule in
  `model.md`.
- Canonical constructor/tag ledger, ordered/unordered collection rules, stable
  structural digest, generated exhaustive visitors/category unions.
- `OWLFactory`/document builder scope with no global mutable identity.
- Structural and OWL 2 DL/profile validation interfaces; implement local model
  constraints and the shared role/global-analysis primitives needed later.
- Source-lexical provenance hooks without putting tag case/prefix/blank labels
  in equality.
- Machine-generated W3C production coverage table and one fixture per branch.
- SWRL values remain public only through `pyowl_core.extensions.swrl`; the
  closed canonical registry retains their extension tags for round trips.

## Acceptance

- all constructor valid/invalid/equality/hash/annotation/visitor/canonical tests;
- permutations, duplicates, chains, cardinality extremes and nested values;
- language tags canonicalize lowercase while raw spelling remains attachable;
- blank-node symmetric alpha-canonical algorithm passes independent goldens;
- no model import depends on io/backend/consumer and stdlib-only leaf policy;
- 100% constructor dispatch coverage and clean schema generation; and
- WP02 receives stable constructors/tags/visitor interfaces.
