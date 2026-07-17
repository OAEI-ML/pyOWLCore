# pyowl-core

`pyowl-core` is the specification and implementation scaffold for a shared,
Java-free OWL 2 structural kernel for Python. It is intended to parse an
ontology once and let Exact-OM, pyELK, pyHermiT, pyOwl2Vec-Star-projector, and
OAEI-Bio-ML-eval reuse the same immutable snapshot without converting through
OWLAPI objects, RDF triples, or files.

This repository is currently **spec-first**. It defines the contracts an
implementation must satisfy; it does not yet claim parser or OWL 2 conformance.

## Product boundary

`pyowl_core` owns:

- the complete OWL 2 structural object model;
- parsing, rendering, document identity, import resolution, and provenance;
- immutable ontology documents, resolved snapshots, and change overlays;
- reusable signatures, axiom indexes, structural views, and fingerprints;
- a stable, validated wire/cache representation; and
- equivalent optimized Rust and compiler-free Python backends.

It deliberately does **not** own reasoning. pyELK compiles a snapshot to its EL
index/saturation IR; pyHermiT compiles it to normalization, DL clauses, and
tableau state; projectors compile it to graph edges. Those products must never
be inserted into the shared structural model.

## Target API

```python
from pyowl_core import ImportPolicy, LoadOptions, load_snapshot

snapshot = load_snapshot(
    "mondo.owl",
    options=LoadOptions(imports=ImportPolicy.RESOLVE_STRICT),
)

# Passing this exact object to another package performs no parsing or copying.
reasoner = pyelk.Reasoner(snapshot)
edges = pyowl2vec_star.project(snapshot)
```

Standalone consumers also accept paths/bytes and call
`pyowl_core.coerce_snapshot(...)`. Caller-owned streams additionally pass an
explicit `document_iri` (and `LoadOptions.format` for text streams); they are
read once and remain open. In-process integrations expose
`SnapshotProvider.owl_snapshot()`. Cross-process handoff uses the versioned
wire format, never pickle.

## Specifications

Start with [`specs/SPEC.md`](specs/SPEC.md) and the work-package dependency
graph in [`specs/workpackages/manifest.toml`](specs/workpackages/manifest.toml).

## Compatibility target

- Distribution: `pyowl-core`
- Import: `pyowl_core`
- Python: 3.10 and newer
- Runtime/build: no Java, JVM, JPype, OWLAPI, ROBOT, or Java archives
- Project source license: Apache License 2.0 (approved third-party artifact
  notices and terms still apply)

## Standards baseline

The normative model follows the
[OWL 2 Structural Specification](https://www.w3.org/TR/owl2-syntax/), with
syntax conversion governed by the
[OWL 2 Mapping to RDF Graphs](https://www.w3.org/TR/owl2-mapping-to-rdf/).
Profiles and conformance are tested against the corresponding W3C
Recommendations. See [`specs/references.md`](specs/references.md).

## Status

No release should be published until all release gates in
[`specs/verification.md`](specs/verification.md) pass, including reservation or
confirmed ownership of the provisional PyPI project name `pyowl-core`.
