# API guide

`pyowl_core.__all__` is the reviewed top-level surface. Complete constructor
families remain available through `pyowl_core.model`; structural index types
remain available through `pyowl_core.index`; consumer negotiation lives in
`pyowl_core.adapters`. Private native, parser, arena, and wire-layout modules
are not public contracts. For task-oriented, runnable introductions to this
surface, start with [getting started](getting-started.md).

## Versions

| Name | Current value | Changes when |
|---|---:|---|
| `__version__` | `0.2.0` | package/API release changes |
| `API_VERSION` | `(0, 2)` | public contract line changes |
| `MODEL_SCHEMA_VERSION` | `2` | equality/canonical/fingerprint semantics change |
| `WIRE_FORMAT_VERSION` | `(1, 2)` | wire compatibility changes |
| `ADAPTER_PROTOCOL_VERSION` | `1` | provider/plugin handshake changes |
| Encoded structural schema | `pyowl-core/structural-columns` v2 | bulk-column meaning changes |

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

The exact entry-point signatures are:

```python
def parse_document(
    source: DocumentSource,
    *,
    format: DocumentFormat | str | None = None,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
) -> OntologyDocument: ...

def load_snapshot(
    source: DocumentInput,
    *,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot: ...

def coerce_snapshot(
    source: OntologyInput,  # DocumentInput | OntologyView | SnapshotProvider
    *,
    document_iri: IRI | str | None = None,
    options: LoadOptions | None = None,
    resolver: ImportResolver | None = None,
    cancellation_token: CancellationToken | None = None,
) -> OntologyView: ...
```

`document_iri` supplies the base/identity of an unparsed root source; it is
required for streams and rejected for an existing document or view.
`LoadOptions` fields and their defaults: `format=None` (autodetect),
`imports=ImportPolicy.RESOLVE_LOCAL`, `backend=BackendPreference.AUTO`,
`limits=ParseLimits()`, `offline=True`, `preserve_source_map=False`,
`collect_provenance=True`, `validate_owl2_dl=False`, `deterministic=True`,
and `allow_partial_rdf_mapping=False`.

`DocumentSource`, `DocumentInput`, and `OntologyInput` are typing aliases, not
runtime base classes. A plain string is a path, never ontology text or a URL.

`LoadOptions.allow_partial_rdf_mapping` defaults to `False`. Setting it to
`True` is a diagnostic-only, one-document mode for explicitly selected RDF/XML
or Turtle input: `parse_document` may then return a document whose
`rdf_mapping_report.conformant` is false and whose `dropped_triples` count is
nonzero. The option is rejected for format autodetection and non-RDF formats.
It is also rejected by `load_snapshot` and `coerce_snapshot`; a nonconformant
diagnostic document cannot enter snapshot, cache, wire, or reasoner routes.

## Views and changes

- `OntologyView` is the read-only consumer protocol. It promises
  `iter_axioms()`, `iter_extensions()`, `contains(axiom)`, `signature`,
  `ontology_annotations`, `is_complete`, `capabilities`, `report`,
  `origin_index`, the `structural_fingerprint` / `logical_fingerprint` /
  `signature_fingerprint` properties, and `view(IndexType, **options)`.
  `OntologySnapshot` additionally exposes the concrete closure: `root`,
  `documents`, `iter_documents()`, `import_manifest`, `diagnostics`,
  `load_options`, `resolution_attempts`, `timings`, and `owl2_dl_report`.
- `OntologyDelta` is a canonical immutable change set.
- `apply_delta` creates a persistent `OntologyOverlay` without copying its base.
- `compose_views` creates an `OntologyComposite` retaining member identity and
  optional roles/bridge delta.
- Structural, logical, and signature fingerprints have separate cache domains.

See [view architecture](views-and-architecture.md) before using concrete fields.

## Structural indexes

`view.view(IndexType, **options)` builds or reuses a lazy structural index.
The public index families and their request types are `SignatureView`,
`DeclarationIndex`, `AnnotationAssertionIndex`, `AxiomTypeIndex`,
`EntityReferenceIndex`, `ExpressionOccurrenceIndex`,
`AssertedClassHierarchyView`, `AssertedPropertyHierarchyView`,
`PropertyDomainRangeView` (with domains/ranges, inverses, and property
chains), and `OntologyIdentityIndex`; each accepts a matching `...Options`
type, and cache behavior is controlled through `configure_index_cache`,
`index_cache_report`, and `clear_index_cache`. They expose asserted structure
only; inferred taxonomy and realization remain reasoner-owned.

## Bulk structural handoff

`EncodedStructuralView` is the public request type for the advertised v2
structural-columns schema. Consumers can key compatibility and provenance
without building a view by reading either
`pyowl_core.ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2` or the identical
`EncodedStructuralView.DESCRIPTOR_SHA256` immutable 32-byte digest. Generic
v2 requests use `EncodedStructuralViewV2`.

The exported digest is schema metadata, not proof that a particular view can
produce buffers. Consumers negotiate
`CoreCapabilities.encoded_view_schemas["pyowl-core/structural-columns"] >= 2`;
absence is the normal scalar-fallback case. The v1 descriptor remains exported
for historical inspection, but a model-schema-2 runtime rejects v1 publication
instead of reinterpreting those columns.

## Wire and caches

- `encode_snapshot(view, *, limits=None, cancellation_token=None) -> bytes`
  and `decode_snapshot(data, *, limits=None, verify=True,
  cancellation_token=None) -> OntologySnapshot` provide validated in-memory
  transport.
- `write_snapshot(view, path, *, atomic=True, durability=DurabilityPolicy.DATA,
  ...) -> Fingerprint` and `open_snapshot(path, *, mmap=True, verify=True, ...)
  -> OntologySnapshot` provide durable and mmap-backed handoff.
- `WireCache(root, *, limits=None, durability=...)` manages versioned entries
  under explicit durability/retention: `publish`, `open`, `get_or_publish`
  (returns a closeable, context-managed `MappedOntologySnapshot`), and
  `collect`.

Unknown required features or incompatible schemas fail closed. Pickle is not a
supported interchange format.

## Errors and diagnostics

All public failures derive from `PyOWLCoreError`; warnings derive from
`PyOWLCoreWarning`. Stable families distinguish syntax, format detection,
imports/resolvers, access/integrity, limits/cancellation, model/profile,
snapshot lifecycle, delta/options, adapters/backends, and wire versions or
corruption. `Diagnostic` carries stable severity/code/message and optional
source spans/details. Do not branch on message text.

Strict `RDF_MAPPING_INCOMPLETE` failures expose the bounded first-pass report as
`UnsupportedSyntaxError.rdf_mapping_report`; consumers do not need a second
partial parse. Each unconsumed `RDFTripleEvidence` includes an `object_kind` of
`"iri"`, `"blank"`, or `"literal"`. Reification failures remain strict and
carry bounded structural fields in `error.as_diagnostic().details`, including
`main_triple_present=False` when the asserted main triple is absent. Their
bounded examples are available as the immutable `error.reification_evidence`
tuple. `reification_issue_count`, `reification_evidence_count`, and
`reification_suppressed_count` reconcile the complete issue count with the
examples retained under `max_diagnostics` and the aggregate evidence-size
bound.

## Native fallback

`BackendPreference.AUTO` may select the private verified extension. If it is
unavailable or incompatible, the complete Python path is selected and
`NativeBackendUnavailableWarning` is emitted once when accelerated work is
requested. `PYTHON` is silent and explicit; `NATIVE` raises instead of falling
back. No public value is a PyO3/Rust object.
