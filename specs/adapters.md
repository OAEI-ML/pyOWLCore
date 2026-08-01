# Consumer adapters and extension points

## 1. Adapter philosophy

An adapter translates a shared structural `OntologyView` into a consumer's
private concepts. It never creates a second OWL model or parser. Standalone
consumer entry points may accept paths/bytes for convenience, but their first
step is `pyowl_core.coerce_snapshot`; an already-loaded view retains identity.

For high-throughput compilation, a consumer first negotiates
`pyowl-core/structural-columns` and requests `EncodedStructuralView` from the
same identity. Scalar iteration remains a complete fallback. Encoded access
does not permit reparsing, in-process wire encode/decode, private native imports,
or persistence of view-local dense IDs.

Core has no runtime dependency on consumers. Adapter code belongs in the
consumer unless it is a syntax-only view useful to multiple consumers.

## 2. Protocol and capability handshake

`ADAPTER_PROTOCOL_VERSION = 1` governs in-process acquisition and optional bulk
structural access. Every `OntologyView` exposes:

```python
@dataclass(frozen=True, slots=True)
class CoreCapabilities:
    adapter_protocol: int
    model_schema: int
    wire_format: tuple[int, int]
    features: frozenset[str]
    encoded_view_schemas: Mapping[str, int]
    backend: str

class OntologyView(Protocol):
    @property
    def capabilities(self) -> CoreCapabilities: ...
    # Read-only members in contracts.md
```

Stable feature names include complete-model constructor families, source-map,
origins, profile validation, mmap, overlay patching, composition, and supported
formats. A consumer declares required features before compilation. Missing or
incompatible features raise `AdapterCompatibilityError` with package/schema
details; they never cause path reparsing or a private fallback parser.

The `0.2.0` transition retains adapter protocol 1 but advertises model schema 2,
wire 1.2, and encoded structural schema 2. Consumers MUST update their supported
schema set and cache keys explicitly. Protocol compatibility alone never
authorizes a consumer to read model-schema-2 identity or schema-2 columns
through a schema-1 decoder.

`SnapshotProvider.owl_snapshot()` returns an `OntologyView` and MUST be:

- idempotent, cheap after initial load, and thread-safe;
- free of import resolution/parsing side effects per call;
- explicit about closed/lifetime state; and
- adapter protocol/model compatible.

`coerce_snapshot` preserves the provider's returned identity. Providers do not
need to subclass a core class.

## 3. Exact-OM adapter

Exact-OM owns an application adapter, tentatively
`ExactOntologySource(SnapshotProvider)`, that stores one core view and exposes
its existing matching-facing `KnowledgeSource` behavior through core views:

| Exact need | Core source |
|---|---|
| typed signature | `SignatureView` |
| labels/annotations/exclusions | `AnnotationAssertionIndex` |
| asserted parents/children | asserted hierarchy views with explicit equivalence policy |
| restrictions/domains/ranges | axiom/expression/domain-range indexes |
| projection | pyOwl2Vec-Star-projector consuming same view |
| reasoning/repair | pyELK/pyHermiT consuming same or overlay/composite view |

`owl_snapshot()` returns the exact stored identity. Exact-OM removes migrated
structural records, parser, projection implementation, and duplicate caches
only after parity tests. Exact's `EntityKind` boundary converts explicitly to
core `EntityKind`; it does not compare stringly typed IRIs and lose punning.

Exact may retain matcher-specific label preference and alignment-exclusion
policies; those are not OWL structural semantics and do not move to core.

## 4. pyELK adapter

pyELK public constructors accept `OntologyInput` and store the resulting
`OntologyView`. Standalone defaults may intentionally use an ELK-compatible
`ImportPolicy.IGNORE` only when documented; passing a view never changes its
manifest. The EL compiler:

1. checks closure/profile policy and core capabilities;
2. scans the encoded structural view in its native performance path, or core
   scalar axioms in its complete fallback path;
3. emits an EL profile/support report for every unsupported constructor;
4. compiles to pyELK's canonical EL indexed IR; and
5. keys that IR by core logical/signature fingerprints plus compiler schema.

pyELK removes its public/shared OWL classes and functional parser after a
deprecation adapter window. Legacy constructors may be aliases/factories that
produce core values, never an alternate equality domain.

Pinned ELK behavior that distinguishes the source case of language tags is
implemented by `ElkCompatibilityKey` in pyELK. It may consult `SourceMap` when
available; core `Literal` equality remains canonical lowercase. Behavior when
source spelling is unavailable (programmatic/wire input) is explicit and tested
against the selected compatibility mode.

Saturation contexts, indexed expressions, rules, taxonomy state, and traces are
private pyELK values and never core views or wire sections.

## 5. pyHermiT adapter

pyHermiT accepts `OntologyInput`/`OntologyView`, then requires:

- `view.view(OntologyIdentityIndex)`, including a complete resolved import
  closure and validation of every document `OntologyID` required by OWL 2 DL;
- complete constructors/capabilities;
- a passing `OWL2DLReport` under its declared datatype policy; and
- canonical language/anonymous identity.

It compiles the shared logical closure into its own normalization, role graph,
DL clauses, and tableau IR. The native performance path consumes the encoded
structural view in coarse batches while retaining its owner; the scalar path is
semantically complete. Core structural fingerprints identify input only;
compiled IR has a pyHermiT schema and private wire format. Incremental updates
may inspect an `OntologyOverlay.delta`, but correctness must match full compile;
a composite is compiled as its effective view.

No pyHermiT parser, ontology model, document resolver, or structural wire
remains after migration except time-bounded compatibility imports that delegate
to core. HermiT-specific OWL 2 DL diagnostics may enrich the core report but do
not mutate it.

## 6. pyOwl2Vec-Star-projector adapter

The projector accepts `OntologyInput` and immediately obtains an
`OntologyView`. Its native performance path uses `EncodedStructuralView` and
structural indexes in coarse batches to compile its private projection plan;
the Python fallback may use scalar access. It produces edge values/buffers
defined by the projector.
Projection choices (bidirectionality, literal inclusion, taxonomy handling,
annotation predicates) are not core semantics.

Its cache key uses `structural_fingerprint` when annotations/literals affect
edges, otherwise the documented minimal logical/signature fingerprints, plus
projector algorithm/plan schema and options. An overlay can patch projection
when proven; a composite is projected without materializing.

Portable artifact provenance reads `OntologyIdentityIndex.document_keys`,
`import_manifest_digest`, and `loader_diagnostics_digest`. Those values are
identical for direct, decoded, and mmap forms of the same view. Honest transport
labels use capability features `wire-v1` and `wire-verified` on decoded/mapped
snapshots, remain outside semantic artifact bytes, and never require inspecting
concrete snapshot fields.

The projector never imports mOWL, Scala, OWLAPI, JPype, or a private core native
module. Its Rust/C acceleration is independently packaged and consumes core
public bulk/wire contracts rather than linking to core's internal arenas.

## 7. OAEI-Bio-ML-eval adapter

Evaluation loaders parse source and target once through core or accept Exact-OM
providers. Ordinary P/R/F/ranking metrics need only entity/mapping data. For
coherence/repair:

```python
trial = compose_views(
    source_view,
    target_view,
    delta=OntologyDelta(add_axioms=bridge_axioms),
    roles=("source", "target"),
)
result = selected_reasoner(trial).check_consistency()
```

This shares both bases and varies only bridge deltas across trials. The
evaluator never calls ROBOT, DeepOnto, OWLAPI, or passes ontology paths to a
subprocess reasoner. Cross-process workers receive `encode_snapshot` wire bytes
or mapped wire files plus authenticated fingerprints, not original paths or
pickle.

Reasoner selection is explicit by profile/capability: pyELK only when the
effective composite is within its complete supported EL scope; otherwise
pyHermiT (or an explicit policy error). The evaluator does not treat an
incomplete reasoner answer as coherence.

## 8. Compatibility and deprecation adapters

Temporary adapters converting an old consumer object into core structure must:

- live in the consumer, never core;
- issue a versioned deprecation warning;
- convert exactly once and cache the resulting view;
- report lossy/unsupported constructs rather than drop them;
- be excluded from performance claims; and
- have a removal version and tests proving new paths do not use them.

Conversion from Java/OWLAPI objects is not shipped in the Java-free release.
Development-only oracle tools run in isolated environments and exchange neutral
fixtures/results.

## 9. Plugin entry points

Core reserves these groups:

```text
pyowl_core.parsers
pyowl_core.writers
pyowl_core.resolvers
pyowl_core.views
```

Plugin metadata declares name, distribution/version, adapter protocol, model
schema range, format/view schema, capabilities, and security classification.
Plugins are discovered as metadata only and instantiated only by an explicit
name in trusted configuration. Installation order never changes auto detection
or resolver precedence.

Plugins return public core values/reports and obey limits/cancellation. Parser
plugins do not receive resolver credentials unless explicitly authorized.
Failures are normalized but preserve their cause. A plugin cannot register a
built-in name or monkey-patch a constructor/tag.

Reasoner/projector plugins are not core plugin groups; their host applications
own those registries.

## 10. Consumer conformance kit

The package ships a reusable test kit that every consumer/provider runs:

- path/bytes standalone load works through core;
- passed snapshot/overlay/composite/provider preserves exact identity;
- no parser/resolver/wire call occurs for in-process view input;
- typed signatures, punning, annotations, anonymous scopes, and import closure
  survive compilation;
- unsupported constructors produce complete stable diagnostics;
- cache keys include model/fingerprint and consumer schema;
- Python-only and native core produce equal consumer results;
- overlays/composites match materialized results;
- closed/incompatible/limited views fail before compilation; and
- static/runtime dependency scans show no private-core or Java imports.

The integration suite includes instrumentation counters so “zero reparse” is an
assertion, not an inference from elapsed time.
