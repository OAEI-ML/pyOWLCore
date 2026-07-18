# Changelog

All notable user-visible changes are recorded here. The project follows
Semantic Versioning for the package/API independently of its model, wire, and
adapter protocol versions.

## Unreleased — 0.1.0 development candidate

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

### Changed

- Wire v1 minor version is `(1, 1)`; the model schema remains `1` and adapter
  protocol remains `1`.
- `OntologySnapshot`, `OntologyOverlay`, and `OntologyComposite` are sibling
  implementations of `OntologyView`; an overlay or composite is not coerced
  into a materialized snapshot.

### Security

- Import resolution defaults to local-only and offline behavior.
- Parsers, resolvers, indexes, overlays, and wire readers enforce explicit
  limits and cancellation before attacker-controlled allocation.
- Plugin discovery is metadata-only until a trusted name is explicitly chosen.

### Release blockers

- No public release is claimed until the artifact, legal, SBOM, provenance,
  PyPI-name, TestPyPI, signing, and reference-machine gates are complete.
- Existing consumer constraints currently target `pyowl-core>=0.1,<0.2`; a
  future 1.0 package version requires coordinated consumer range updates.

