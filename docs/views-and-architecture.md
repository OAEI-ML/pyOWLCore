# Documents, snapshots, views, overlays, and composites

These names describe different ownership and lifecycle contracts.

| Value | Meaning | Imports | Storage |
|---|---|---|---|
| `OntologyDocument` | one parsed source with direct imports | recorded, not resolved | immutable document |
| `OntologySnapshot` | materialized resolved closure | fixed manifest/policy | immutable closure |
| `OntologyView` | read-only protocol shared by consumers | supplied by implementation | no concrete layout promise |
| `OntologyOverlay` | persistent delta over a base view | inherited | base identity plus patch |
| `OntologyComposite` | source/target/member union plus optional bridge | each member preserved | strong references, no base copy |

An overlay or composite is not a snapshot subclass and must not be silently
materialized merely to satisfy a consumer type annotation. Consumers capable
of repair trials accept `OntologyView`; operations that truly require a
materialized closure accept `OntologySnapshot` explicitly.

## Structural versus consumer IR

Core retains asserted OWL structure, provenance, fingerprints, and structural
indexes. It never stores:

- pyELK saturation rules, contexts, or taxonomies;
- pyHermiT normalization, clauses, dependencies, blocking, or tableau state;
- OWL2Vec* projection plans or edge buffers;
- Exact-OM label preference/candidate features; or
- evaluation metric/reasoner sessions.

Each consumer compiles the shared view once and keys private IR by the relevant
core fingerprint, core schemas, its compiler schema, and semantic options.

## Identity-preserving handoff

`coerce_snapshot(view) is view`. For a `SnapshotProvider`, the exact object
returned by `owl_snapshot()` is validated and retained. Passing resolver,
format, or root document-IRI options to an existing view raises an option
conflict; it never reparses or rebases the object.

Wire decode produces a structurally equal but distinct view for process/cache
boundaries. Snapshot-local integer IDs, buffer offsets, and mmap addresses are
never public identity.

