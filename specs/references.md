# References and evidence baseline

The implementation must pin the exact revision/date of every imported test
corpus or development-only oracle. A URL alone is not provenance.

## Normative OWL 2 references

- [OWL 2 Structural Specification and Functional-Style Syntax, Second Edition](https://www.w3.org/TR/owl2-syntax/)
- [OWL 2 Mapping to RDF Graphs, Second Edition](https://www.w3.org/TR/owl2-mapping-to-rdf/)
- [OWL 2 XML Serialization, Second Edition](https://www.w3.org/TR/owl2-xml-serialization/)
- [OWL 2 Profiles, Second Edition](https://www.w3.org/TR/owl2-profiles/)
- [OWL 2 Direct Semantics, Second Edition](https://www.w3.org/TR/owl2-direct-semantics/)
- [OWL 2 Conformance, Second Edition](https://www.w3.org/TR/owl2-conformance/)
- [RDF 1.1 Concepts and Abstract Syntax](https://www.w3.org/TR/rdf11-concepts/)
- [rdf:PlainLiteral, Second Edition](https://www.w3.org/TR/rdf-plain-literal/)
- [RDF 1.1 Turtle](https://www.w3.org/TR/turtle/)
- [RDF 1.1 XML Syntax](https://www.w3.org/TR/rdf-syntax-grammar/)
- [BCP 47 language tags](https://www.rfc-editor.org/info/bcp47)
- [RFC 3987 Internationalized Resource Identifiers](https://www.rfc-editor.org/rfc/rfc3987)
- [SWRL W3C Member Submission](https://www.w3.org/Submission/SWRL/) (optional
  extension only; not part of the OWL 2 structural axiom grammar)

OWL 2 errata current at release time MUST be reviewed, captured in a checked-in
decision ledger, and covered by a regression where applicable.

## Primary implementation references

- [Horned-OWL crate documentation](https://docs.rs/horned-owl/)
- [Horned-OWL model documentation](https://docs.rs/horned-owl/latest/horned_owl/model/)
- [py-horned-owl documentation](https://ontology-tools.github.io/py-horned-owl/)
- [Horned-OWL source](https://github.com/ontology-tools/horned-owl)
- [py-horned-owl source](https://github.com/ontology-tools/py-horned-owl)
- [Horned-OWL design and performance evaluation](https://doi.org/10.4230/TGDK.2.2.9)
- [OWLAPI source](https://github.com/owlcs/owlapi)
- [OWLAPI documentation](https://owlcs.github.io/owlapi/)

These are implementation candidates and differential comparators, not semantic
authorities. Their output never overrides a W3C requirement or a documented
core decision. WP14 pins exact comparator revisions, features, artifacts, and
hashes; mutable documentation URLs are discovery references, not benchmark
provenance.

## Workspace consumer references

- `Exact-OM/specs/WP-B-ontology-backend.md`
- `pyELK/specs/SPEC.md`, `contracts.md`, and `parsing.md`
- `pyHermiT/specs/SPEC.md`, `ontology-model.md`, and `contracts.md`
- the projector and evaluator migration specifications that consume this API

Before implementation begins, record the commit IDs inspected when these core
specifications were designed. Future compatibility updates must cite the new
consumer revisions rather than relying on mutable branch heads.
