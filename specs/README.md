# Specification index

The specifications are normative implementation contracts, not descriptions of
already-delivered functionality. Begin with the master specification.

| Document | Subject |
|---|---|
| [`SPEC.md`](SPEC.md) | product scope, invariants, compatibility, definition of done |
| [`architecture.md`](architecture.md) | layers, ownership, lifecycle, structural/consumer IR boundary |
| [`contracts.md`](contracts.md) | public types, loaders, views, errors, wire functions |
| [`model.md`](model.md) | complete OWL 2 structural constructors and canonical identity |
| [`parsing-imports.md`](parsing-imports.md) | formats, writers, imports, resolvers, provenance |
| [`snapshots-overlays.md`](snapshots-overlays.md) | immutable closure, deltas, overlay and zero-copy composition |
| [`indexes-views.md`](indexes-views.md) | reusable syntax-only indexes and bulk views |
| [`wire-format.md`](wire-format.md) | PYOCORE v1 cache/IPC bytes, mmap and validation |
| [`adapters.md`](adapters.md) | Exact-OM, pyELK, pyHermiT, projector and evaluator integration |
| [`native-backend.md`](native-backend.md) | Rust acceleration and complete Python fallback |
| [`security.md`](security.md) | hostile input, resource, filesystem, network and supply-chain policy |
| [`performance.md`](performance.md) | large biomedical benchmarks and regression gates |
| [`packaging.md`](packaging.md) | Python 3.10, pure/native artifacts, license/Java/name release gates |
| [`verification.md`](verification.md) | W3C, differential, fuzz, consumer and release tests |
| [`references.md`](references.md) | normative and implementation evidence sources |
| [`workpackages/`](workpackages/) | dependency-ordered implementation briefs and ownership manifest |

Specification precedence and change control are defined in `SPEC.md`. A public
contract change updates every affected focused spec, test/golden, version
decision, and consumer specification in the same coordinated change.

