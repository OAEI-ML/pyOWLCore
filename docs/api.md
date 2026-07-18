# API guide

`pyowl_core.__all__` is the reviewed top-level surface. Complete constructor
families remain available through `pyowl_core.model`; structural index types
remain available through `pyowl_core.index`; consumer negotiation lives in
`pyowl_core.adapters`. Private native, parser, arena, and wire-layout modules
are not public contracts.

## Versions

| Name | Current value | Changes when |
|---|---:|---|
| `__version__` | `0.1.0.dev0` | package/API release changes |
| `API_VERSION` | `(0, 1)` | public contract line changes |
| `MODEL_SCHEMA_VERSION` | `1` | equality/canonical/fingerprint semantics change |
| `WIRE_FORMAT_VERSION` | `(1, 1)` | wire compatibility changes |
| `ADAPTER_PROTOCOL_VERSION` | `1` | provider/plugin handshake changes |

Do not compare package versions lexically. Persisted consumer cache keys also
include the consumer compiler schema and semantic options.

## Model

The top level exports every model constructor listed by `pyowl_core.model.__all__`,
including IRIs, typed entities, literals, annotations, class/data expressions,
property expressions, all OWL 2 axiom families, factories, visitors, canonical
encoding, and structural digests. SWRL is explicitly namespaced under
`pyowl_core.extensions.swrl` and is not mislabeled as OWL 2 axioms.

Model values are immutable. Equality is syntax-independent canonical structural
identity, not RDF node identity, a reasoner ID, or object address.

## Acquisition and loading

- `parse_document(source, ...) -> OntologyDocument` parses exactly one source.
- `load_snapshot(source, ...) -> OntologySnapshot` resolves a closure.
- `coerce_snapshot(source_or_view, ...) -> OntologyView` preserves an existing
  view/provider identity and parses only acquisition inputs.
- `DocumentFormat`, `ImportPolicy`, `BackendPreference`, `LoadOptions`, and
  `ParseLimits` make all policy and resource choices explicit.

`DocumentSource`, `DocumentInput`, and `OntologyInput` are typing aliases, not
runtime base classes. A plain string is a path, never ontology text or a URL.

## Views and changes

- `OntologyView` is the read-only consumer protocol.
- `OntologyDelta` is a canonical immutable change set.
- `apply_delta` creates a persistent `OntologyOverlay` without copying its base.
- `compose_views` creates an `OntologyComposite` retaining member identity and
  optional roles/bridge delta.
- Structural, logical, and signature fingerprints have separate cache domains.

See [view architecture](views-and-architecture.md) before using concrete fields.

## Structural indexes

`view.view(IndexType, **options)` builds or reuses a lazy structural index.
Public index families cover signatures, declarations, annotations, axiom
types, entity references, expression occurrences, asserted class/property
hierarchies, domains/ranges, inverse/property chains, and ontology identities.
They expose asserted structure only; inferred taxonomy and realization remain
reasoner-owned.

## Wire and caches

- `encode_snapshot` and `decode_snapshot` provide validated in-memory transport.
- `write_snapshot` and `open_snapshot` provide durable and mmap-backed handoff.
- `WireCache` manages versioned entries under explicit durability/retention.

Unknown required features or incompatible schemas fail closed. Pickle is not a
supported interchange format.

## Errors and diagnostics

All public failures derive from `PyOWLCoreError`; warnings derive from
`PyOWLCoreWarning`. Stable families distinguish syntax, format detection,
imports/resolvers, access/integrity, limits/cancellation, model/profile,
snapshot lifecycle, delta/options, adapters/backends, and wire versions or
corruption. `Diagnostic` carries stable severity/code/message and optional
source spans/details. Do not branch on message text.

## Native fallback

`BackendPreference.AUTO` may select the private verified extension. If it is
unavailable or incompatible, the complete Python path is selected and
`NativeBackendUnavailableWarning` is emitted once when accelerated work is
requested. `PYTHON` is silent and explicit; `NATIVE` raises instead of falling
back. No public value is a PyO3/Rust object.

