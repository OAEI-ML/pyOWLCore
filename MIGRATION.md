# Migration to pyowl-core

## Updating from 0.1.0 to 0.1.1

No public API, model, wire, adapter, or encoded-view migration is required.
Upgrade the complete distribution version together; do not combine the
published `0.1.0` universal files with `0.1.1` native wheels.

## Purpose

Move shared OWL parsing and structural values into one `OntologyView`. Keep
reasoner normalization, saturation/tableau state, projector edges, matcher
features, and evaluation metrics in their owning packages.

## Replace path handoffs

Before:

```text
consumer_a(path) -> private parse
consumer_b(path) -> second private parse
```

After:

```python
from pyowl_core import coerce_snapshot

view = coerce_snapshot(source_or_existing_view)
consumer_a(view)
consumer_b(view)
```

Standalone APIs may still accept paths or bytes, but their first acquisition
step must be `coerce_snapshot`. Existing views and providers are returned by
identity and must not be serialized, copied, resolved again, or converted via
RDF triples.

## Type migration

| Legacy/shared concern | Stable core replacement |
|---|---|
| Package-specific IRI/entity/literal | `IRI`, typed `Entity`, `Literal` |
| Private OWL axiom/expression classes | `pyowl_core.model` constructors |
| Parsed file pretending to be a closure | `OntologyDocument` then `load_snapshot` |
| Mutable repair copy | `OntologyDelta` plus `OntologyOverlay` |
| Source/target concatenation | `compose_views(..., roles=...)` |
| Pickle/path IPC | `encode_snapshot`, `decode_snapshot`, `open_snapshot` |
| Ad-hoc parser capability check | `pyowl_core.adapters` negotiation |

Entity identity includes both entity kind and IRI, so code that keyed only by
IRI must explicitly preserve OWL punning. Literal language tags use canonical
lowercase identity; source spelling belongs to `SourceMap`, not shared equality.
Anonymous individuals are document-scoped and cannot be moved between
documents without explicit re-scoping.

## Imports and security

`parse_document` parses one source and never resolves imports. `load_snapshot`
constructs a closure under `LoadOptions.imports`. The default is offline and
local-only. Network acquisition requires an explicitly configured resolver;
never translate an ontology IRI directly into an unrestricted URL fetch.

Passing format, resolver, or root `document_iri` options to an existing view is
an error, not a request to rebuild it. Caller-owned streams remain open and are
read once.

## Consumer-specific boundary

- Exact-OM retains matching policies and exposes its stored view through
  `owl_snapshot()`.
- pyELK compiles the view into EL indexes and saturation state.
- pyHermiT compiles it into normalized clauses and tableau state.
- The OWL2Vec* projector compiles it into its private edge plan/buffers.
- OAEI evaluation composes source, target, and bridge deltas, then selects a
  reasoner explicitly by supported profile.

None of these migrations moves reasoner or projector IR into core.

## Compatibility and removal policy

The current tested workspace line requires `pyowl-core>=0.1,<0.2`, API `(0,1)`,
model schema `1`, wire major `1`, and adapter protocol `1`. Compatibility
adapters live in consumers, convert once, warn with a removal version, and must
report loss rather than silently dropping constructs. Java/OWLAPI conversion
is not shipped in the Java-free runtime.

Review [the exact compatibility table](docs/compatibility.md) before widening a
dependency range or reusing a persisted consumer cache.
