# Strict native structural indexes

These opt-in indexes preserve their existing asserted/query semantics and retain
immutable native component storage. General default constructors remain unchanged.
Use `ViewType.supports_native()` before loading data, then request
`owner.view(ViewType, require_native_pipeline=True, include_origins=False)`.
A positive capability probe does not make an unsupported owner valid.

- `AnnotationAssertionIndex` builds native subject/property postings and publishes
  only requested annotation rows through `iter_columns`. It supports direct native
  snapshots (including imported scopes), and explicit native-backed overlay deltas.
  Nested occurrence queries remain unsupported in strict mode. Full canonical axiom
  identity, lexical form, language, datatype and anonymous scope are preserved.
- `AssertedClassHierarchyView` constructs named edges, equivalence components and
  adjacency in native code. Equivalence and disjoint-union options keep their old
  meanings. `AssertedPropertyHierarchyView` shares that machinery and preserves
  separate inverse/chain records. Its additive `component`, `direct_parents`, and
  `direct_children` methods require component mode for directness. Direct parents
  remove a candidate reachable from another candidate; direct children are its
  inverse. Cycles retain this definition rather than being silently collapsed.
- `AxiomTypeIndex` retains native constructor and exact-entity postings.
  `iter`, `tuple`, and `count` accept `referencing=Entity | None`. A matching root
  occurs once even if the entity repeats; entity kind is part of identity, so
  class/individual and object/data property punning remain distinct. Categories
  and all-root iteration keep canonical root order. Paging merges native postings
  by root cursor; Python receives requested full axiom wrappers only.

- `PropertyDomainRangeView` indexes all six object/data/annotation domain/range
  constructors, preserving inverse expressions, punning, complex values, annotations,
  and named-only filtered counts. Construction encodes only property keys; native
  property/count queries and forward-only pages decode requested axiom wrappers.

Hierarchy and domain/range views can reuse an overlay's base index only when its explicit delta
contains no relevant changed constructor (ROOT/DOCUMENT selection is unchanged).
Strict typed indexes permit nonannotation queries on annotation-only overlays;
annotation queries there fail explicitly and can use `AnnotationAssertionIndex`.
Other transformed/decoded/mmap owners and logical typed-index deltas are currently
unsupported; strict mode raises before scalar construction.

`AxiomTypeIndex.native_report` is public on the view type; default-mode indexes
raise `BackendProtocolError` rather than claim strict native diagnostics.
`native_report` records actual construction and requested-query counters, with
conservative allocation charges rather than claimed allocator measurements. The
native typed report's `has_object_data_property_punning` checks the entire selected
axiom population, including declaration-only cases. This permits an IRI-only
consumer to reject ambiguous legacy presentation without changing typed semantics.

Construction/query work checks existing limits and cancellation; indivisible
annotation rows exceeding `max_bytes` raise `ResourceLimitError`. Published rows
retain immutable values after owner closure, while further requests follow the
existing owner close contract. Optional origins use selected axiom identities;
overlay provenance requests never construct its full Python origin index.

Bounded differential tests are in `tests/native/encoded_views/test_native_*index.py`,
`test_native_annotation_columns.py`, `test_native_property_domains.py`, and the
native hierarchy tests. Operation
counts distinguish one-time native root scans from selected query visits. These
fixtures establish semantics and scaling structure, not a full-ontology latency
or memory-speedup claim.
