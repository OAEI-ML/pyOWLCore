# Changelog

All notable user-visible changes are recorded here. The project follows
Semantic Versioning for the package/API independently of its model, wire, and
adapter protocol versions.

## 0.1.0 — 2026-07-30

### Added

- Complete immutable OWL 2 structural constructors, validation, canonical
  identity, source provenance, and separately namespaced SWRL structures.
- Functional Syntax, OWL/XML, RDF/XML, and Turtle parsing with deterministic
  rendering and explicit import policies.
- Immutable documents, resolved snapshots, deltas, overlays, composites,
  fingerprints, lazy structural indexes, and versioned wire/mmap transport.
- Complete compiler-free Python behavior and optional private Rust parsing,
  wire, and index acceleration with strict fallback rules.
- Consumer capability negotiation, path-free cache keys, explicit plugin
  metadata discovery, and zero-reparse conformance fixtures.
- Python 3.10 and 3.12 semantic, security, conformance, consumer, and
  performance evidence.
- Explicit universal-pure and required-native PEP 517 build modes, artifact and
  import inspectors, locked dependency/license inventory, deterministic
  pure/native SBOMs, and a checksum-bound fail-closed release report.
- Immutable-candidate CPython 3.10–3.14 native wheel definitions for five
  platform/arch lanes, pure CPython/PyPy resolver lanes, deterministic sdist
  and wheel rebuild checks, source-path remapping, and guarded Trusted
  Publishing promotion that consumes an explicit successful build run.
- Curated API, architecture, consumer handoff, compatibility, migration,
  security, troubleshooting, performance, release, yank, and rollback guidance
  with executable Java-free examples.

### Changed

- Wire v1 minor version is `(1, 1)`; the model schema remains `1` and adapter
  protocol remains `1`.
- Eager wire decoding now reuses fully validated top-level model rows during
  materialization instead of decoding the same canonical axiom payload twice;
  lazy mmap opening remains metadata-only.
- `OntologySnapshot`, `OntologyOverlay`, and `OntologyComposite` are sibling
  implementations of `OntologyView`; an overlay or composite is not coerced
  into a materialized snapshot.
- Packaging metadata uses the PEP 639 Apache-2.0 expression and includes NOTICE
  plus exact third-party license texts/inventory. This is engineering metadata,
  not a claim of completed legal approval.
- Consumer handoff examples now track the current pyELK, pyHermiT, and
  OWL2Vec* projector APIs, including pyELK's explicit completeness result.

### Security

- Import resolution defaults to local-only and offline behavior.
- Parsers, resolvers, indexes, overlays, and wire readers enforce explicit
  limits and cancellation before attacker-controlled allocation.
- Plugin discovery is metadata-only until a trusted name is explicitly chosen.

### Release authorization

- The release owner closed the remaining external gates for the initial
  production publication and authorized local account-token upload.
- Existing consumer constraints currently target `pyowl-core>=0.1,<0.2`; a
  future 1.0 package version requires coordinated consumer range updates.
