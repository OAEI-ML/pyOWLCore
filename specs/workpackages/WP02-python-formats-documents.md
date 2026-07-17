# WP02 — Python formats, documents, writers, and provenance

## Goal

Deliver the complete pure-Python one-document path for required formats,
canonical structural mapping, deterministic writers, and document/source
provenance. This is the semantic fallback and native oracle.

## Read first

`parsing-imports.md` sections 1–5/10, `model.md`, `security.md`, `verification.md`,
the W3C mapping/syntax references, and WP01 handoff.

## Depends on

WP01.

## Owned paths

Python format/source backend, immutable `OntologyDocument`/provenance, and
format/roundtrip tests listed in the manifest. Do not resolve imports or create
snapshots.

## Deliverables

- Streaming source abstraction for path/bytes/BinaryIO/TextIO with exact
  ownership, detection, limit, digest and source-span behavior.
- Complete Functional, OWL/XML, RDF/XML and Turtle readers; RDF reverse mapping
  with consumed/unconsumed triple report and strict/explicit partial behavior.
- Functional/RDFXML writers, then Turtle/OWLXML before 1.0, with deterministic
  canonical mode and explicit lossy policy.
- `OntologyDocument`, source map/origin occurrence structures, document
  fingerprint and canonical blank-node freeze.
- Pure-Python parser diagnostics/recovery separation, cancellation/resource
  accounting, and XML hostile defaults.

## Acceptance

- required W3C syntax/mapping fixture subset and every-constructor cross-format
  parse pass in Python;
- parse-render-parse structural equality and canonical byte/RDF-isomorphism
  properties;
- nonseekable/chunked/path/bytes/TextIO explicit rules and digest provenance;
- malformed, deep, long, XML entity, RDF list, encoding and cancellation cases
  fail within limits without partial valid objects;
- `parse_document` makes zero resolver/network/import opens; and
- WP03 receives stable documents/provenance/source contracts.

