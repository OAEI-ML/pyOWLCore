# Shared indexes and structural views

## 1. Boundary

Core indexes accelerate retrieval of asserted syntax. They do not derive
semantic consequences. An index may answer “which asserted `SubClassOf` axioms
mention class A?” but not “which classes are entailed subclasses of A?”

The following are forbidden in core: EL saturation contexts, HermiT clauses or
tableau indexes, inferred taxonomy caches, projector edge semantics, repair
scores, and matcher feature matrices.

## 2. View factory and cache

```python
view = ontology.view(AxiomTypeIndex)
view = ontology.view(EntityReferenceIndex, include_annotations=False)
```

`OntologyView.view(type, **options)` canonicalizes typed options and keys a
snapshot-local once cache by `(view schema, options)`. Equivalent requests
return the same immutable view identity while alive. Unknown options fail; they
are never silently ignored.

Index construction is lazy. A caller that wants eager builds requests each
view explicitly after load (for example `ontology.view(AxiomTypeIndex)` at
startup); `LoadOptions` deliberately carries no prebuild field, so loading
never hides index-build cost. Reports distinguish already-built, cache-hit,
patched, merged, and full-build cost. A failed/cancelled build publishes
nothing and may be retried.

All results are immutable and deterministic. Default methods return bounded
iterators/views rather than copying millions of entries. A `tuple` convenience
may allocate and is named/documented accordingly.

## 3. Signature view

`SignatureView` indexes typed entities appearing in declarations, expressions,
axioms, annotations, ontology metadata, or rules. Queries choose:

- root, one document, or closure/effective view;
- entity kind or all kinds;
- declared only versus all referenced;
- include implicit OWL built-ins; and
- include annotation-only references.

Punning remains typed. `entities_by_iri(iri)` returns every `Entity` kind. A
flat IRI set is explicit and cannot silently collapse kinds. Results are sorted
by kind tag and full IRI for canonical requests.

## 4. Axiom type index

`AxiomTypeIndex` has exact-constructor and category postings:

```python
index.iter(SubClassOf)
index.iter_category(LogicalAxiom)
index.count(SubClassOf)
```

Subclass matching of Python implementation classes is not the category model;
generated constructor tags/categories are. A new constructor must update the
category table or exhaustive tests fail.

Root/document/closure origin selection is supported without duplicating axiom
objects. Overlay postings patch add/removes. Composite postings k-way merge and
deduplicate while retaining origins.

## 5. Entity reference index

`EntityReferenceIndex` recursively walks every model constructor and maps typed
entity/IRI/anonymous keys to structural occurrences:

```text
ReferenceOccurrence:
  axiom, origin(s), constructor_path, role, polarity_hint?
```

`constructor_path` uses stable field IDs, not Python attribute strings.
`role` distinguishes subject/property/filler/domain/range/annotation/rule
positions. A core `polarity_hint` MAY describe purely syntactic positive/
negative constructor position, but it cannot encode reasoner normalization and
is optional until specified exhaustively.

Queries can exclude annotations or source provenance. Reference indexes are the
basis for module candidates, consumer compilation invalidation, and overlay
patching, not a semantic locality module implementation.

## 6. Declaration and annotation indexes

`DeclarationIndex` maps each typed entity to declaration axioms and origins and
reports undeclared referenced entities without declaring them implicitly.

`AnnotationAssertionIndex` maps:

- annotation subject → assertion postings;
- `(subject, property)` → ordered/canonical values;
- IRI annotation values → reverse postings; and
- nested axiom/annotation occurrences when requested.

Language selection is a query utility using canonical BCP 47 matching and
caller preference order. It does not change `Literal` identity. Exact-OM label
and exclusion adapters should build on this index rather than reparsing RDF.

## 7. Asserted hierarchy views

`AssertedClassHierarchyView` exposes only named endpoints from asserted:

- `SubClassOf(Class, Class)`;
- optional named members of `EquivalentClasses`; and
- optional consumer-selected simple named endpoints derived from
  `DisjointUnion`'s definitional equality.

Options explicitly choose equivalence handling:

- `PRESERVE`: return equivalence sets separately, subclass edges unchanged;
- `BIDIRECTIONAL`: expose pairwise asserted-normalized edges; or
- `COMPONENT`: canonical equivalence component nodes.

No transitive closure/reduction, complex-expression reasoning, unsatisfiability,
or inferred directness is performed. Methods are named `asserted_parents`,
`asserted_children`, and `equivalents`; aliases `direct_parents` are avoided in
core because “direct” is reasoner-dependent. Exact-OM may adapt the component
view to its `KnowledgeSource.direct_parents` asserted contract.

`AssertedPropertyHierarchyView` similarly exposes named
`SubObjectPropertyOf`/`SubDataPropertyOf`, equivalences, inverses, and property
chains as separate structural records. A chain is never flattened into pairwise
subproperty edges.

## 8. Domain/range and expression views

`PropertyDomainRangeView` indexes asserted object/data/annotation property
domain and range axioms. Named-only convenience iterators filter explicitly;
they never drop complex expressions without a count/diagnostic.

`ExpressionOccurrenceIndex` interns canonical expressions within a view and
tracks their containing axioms/paths. It supports bulk traversal needed by
pyELK, pyHermiT, and projection without allocating a Python callback per node.
Public behavior is identical in Python; an experimental bulk encoded iterator
may be negotiated through the adapter capability contract.

## 9. Ontology identity and loader provenance

Consumers that need document-level ontology/version IRIs request the generic
identity view rather than inspecting snapshot, overlay, composite, or mmap
storage:

```python
identity = ontology.view(OntologyIdentityIndex)
identity.documents                 # tuple[OntologyDocumentIdentity, ...]
identity.document_keys             # tuple[str, ...]
identity.import_manifest_digest    # bytes32
identity.loader_diagnostics_digest # bytes32
identity.is_complete               # bool
```

Each document record contains only `document_key` and `OntologyID`. Snapshot
records use exact import-manifest keys. An overlay retains its base records and
digests without copying them. A composite prefixes every member key with the
same `member:<source-token-hex>:` namespace used by origins, then sorts the
result; bridge deltas add no document identity. Composite manifest and loader
diagnostic digests are domain-separated SHA-256 combinations of the member
token/digest pairs. The view never exposes resolver objects, locators, paths,
credentials, timestamps, or mutable manifest state.

For a concrete snapshot, `import_manifest_digest` is SHA-256 of exact
`ImportManifest.canonical_bytes()` semantics. Loader diagnostics are the
ordered `Diagnostic.to_dict()` records serialized as UTF-8 JSON with sorted
keys and compact separators, prefixed by
`pyowl-core:loader-diagnostics:v1\0`, then SHA-256 hashed. The empty diagnostic
sequence therefore has one stable nonzero digest. `is_complete` is the source
view's closure completeness, not an inference from document count.

The built-in schema is `pyowl-core/ontology-identity-index` version 1 and is
advertised by capability feature `ontology-identity-index`. Mapped builds
decode only bounded document/import/provenance metadata and do not materialize
ontology roots, axioms, or terms.

## 10. Bulk access and native acceleration

For large ontologies, public scalar iterators remain correct but consumers may
request `ontology.view(EncodedStructuralView, schema_version=1, ...)`. WP17
freezes schema 1 and its generated descriptor ledger before a stable release:

```text
EncodedStructuralView:
  schema_name = "pyowl-core/structural-columns"
  schema_version: positive integer
  model_schema: exact core model schema
  scope/options: explicit structural selection
  owner: strong reference to originating OntologyView/storage
  buffers: immutable mapping of named read-only byte buffers
  descriptor: canonical field/tag/offset/width/sequence metadata
  structural_fingerprint: content covered by this view
  segments: base/delta/member metadata for overlays/composites
```

All scalar columns use specified little-endian exact-width fields; strings and
variable sequences use checked offset/length columns. Tags come from the model
schema ledger, never a Rust enum discriminant. IDs are dense and valid only
within the retained owner/schema/view; they are not semantic identities and are
not persisted in consumer caches.

This is a core-owned, documented, versioned structural representation—not a
Rust object or consumer IR. The Python fallback produces identical logical
buffers. Consumers validate schema/capabilities, retain the owner, and compile
their private IR in a bounded number of coarse calls. The native/mmap backend
MUST expose matching owned columns without ontology-sized copying where its
layout already satisfies the schema. The stable wire format remains the cross-
process alternative; in-process consumers do not encode/decode wire merely to
obtain this view.

Overlay/composite views prefer a small segment table referencing base encoded
views plus canonical add/remove/member postings. A consumer may request an
explicit materialized encoded view, but the default repair path cannot hide a
base-sized flattening copy.

Never expose a raw pointer, mutable NumPy view, borrowed PyO3 buffer that outlives
the call, machine-width enum, or native-endian struct. Optional array libraries
belong in consumer adapters, not core runtime dependencies.

## 11. Memory accounting and eviction

Every view reports estimated/actual bytes by major table. Snapshot caches hold
strong or bounded weak/LRU references according to `IndexCachePolicy`; default
strong caching is bounded by `max_index_bytes`. Eviction changes only future
cost, never view behavior while a caller holds it.

Building an index reserves accounted memory before growth. Limit failure raises
`ResourceLimitError` and leaves other views usable. Composite/overlay indexes
prefer posting adapters over duplicating bases; benchmark gates enforce this.

## 12. Extension views

Third parties may define `StructuralViewFactory` plugins with a globally unique
name, schema version, typed immutable options, dependency list of core views,
and structural/logical fingerprint selection. Plugins load only when explicitly
requested. They cannot insert values into core caches under a built-in type/name
or access native internals.

Consumer-private views should remain in the consumer. A view belongs in core
only when at least two consumers need the same syntax-only result and its full
semantics/fallback/resource behavior are specified here.

## 13. Acceptance gates

- every model constructor is covered by signature/reference walking;
- results equal independent full scans on generated ontologies;
- root/document/closure, annotation, punning, built-in, and origin options pass;
- asserted hierarchy cases prove no accidental inference/transitive reduction;
- overlay/composite indexes equal materialized indexes without base-sized copy
  for small deltas;
- simultaneous build/cancel/evict/read is race- and leak-free;
- Python/native deterministic and semantic parity passes;
- encoded structural buffers cover every constructor, match scalar traversal,
  retain their owner safely, and require no per-axiom Python callback;
- native direct/mmap publication performs no ontology-sized copy, while Python
  fallback buffer construction is measured and explicit;
- index bytes and build latency are reported in biomedical benchmarks; and
- consumer import checks forbid reasoner/projector IR in `pyowl_core.index`.
