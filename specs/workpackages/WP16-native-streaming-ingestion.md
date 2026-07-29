# WP16 — native streaming parsing and structural mapping

## Goal

Feed the retained arena directly from complete native syntax parsers and OWL
structural mapping, eliminating RDFLib/Python triple graphs and native-to-Python
reconstruction from the optimized load path.

## Read first

`native-ontology-redesign.md`, `native-backend.md`, `parsing-imports.md`,
`model.md`, `security.md`, `verification.md`, and the WP02/WP03/WP15 handoffs.

## Depends on

WP02, WP03 and WP15.

## Owned paths

Native source/session/parser/RDF-mapping/canonical-ingestion and ingestion-
binding modules, Python acquisition/API/format routing, and format/backend
differential tests listed in the manifest. Resolver policy and callbacks remain
Python-owned contracts. WP15's arena/facade/dispatch seams are a frozen handoff;
changes require an explicit WP15 ownership handoff rather than an overlapping
edit with WP17.

Closure orchestration in `api.py`/`document/imports.py` may schedule and freeze
documents, but it publishes the final facade only through WP15's
`NativeSnapshotPublicationV1`. It does not construct or mutate private fields in
`document/snapshot.py`, `facade.rs`, overlay/composite storage, or WP17 views.

## Deliverables

- Complete streaming RDF/XML parser and native OWL 2 RDF mapping into the arena,
  followed by Turtle, OWL/XML, and Functional Syntax in profile-informed order.
- Native ontology header/import/annotation/list/blank-node/rule handling,
  canonical freeze, diagnostics/source positions, limits, cancellation, and
  deterministic bounded parallel parsing of independent documents.
- Python resolver orchestration that passes owned bytes/documents and assembles
  a retained native closure without flattening imported axioms.
- Import diamonds, cycles, partial failure, cancellation, and deterministic
  parallel assembly executed against WP15's generated publication fake and the
  real retained arena, proving the frozen builder-to-snapshot boundary is the
  only closure publication path.
- Capability dispatch that advertises a format only after all constructors and
  negative/error cases for that top-level operation pass forced native.
- Installed-wheel, forced-native end-to-end fixtures for every advertised
  format, crossing real bytes through mapping/freeze/snapshot/facade and an
  encoded-view or scalar consumer operation. They include interacting
  constructors, malformed input, limits, cancellation, import/provenance, and
  the retained cross-layer regression corpus; unit tests alone cannot enable a
  capability bit.
- Differential, W3C, corpus, round-trip, hostile, fuzz, memory, and source-map
  evidence for every advertised format.

## Acceptance

- Forced native matches the complete Python backend on canonical values,
  fingerprints, wire, import manifests, diagnostics, and required writers for
  every advertised format/option.
- Capability reports are asserted absent before the complete installed-path
  matrix and present only afterward. The suite disables fallback and detects a
  native request that quietly executes Python.
- RDF native loading constructs neither an RDFLib graph nor a Python triple/
  axiom collection proportional to input and performs no eager complete Python
  model conversion.
- Unsupported native capability is rejected/selected before input consumption;
  there is no halfway fallback or native call into Python semantic handlers.
- Shared-import diamonds parse each content digest once; anonymous scopes,
  imports, provenance, limits, cancellation, and deterministic parallel results
  pass.
- Phase/object/allocation reports expose parsing, mapping, arena construction,
  freeze, closure, and publication separately for WP18 optimization.
