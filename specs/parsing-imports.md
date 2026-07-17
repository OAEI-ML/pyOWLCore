# Parsing, rendering, imports, and provenance

## 1. One-document parse contract

`parse_document` parses exactly one source. It may validate the direct import
declarations syntactically, but it MUST NOT call a resolver, open an import, or
construct a closure. This invariant makes parsing reproducible and lets pyELK,
pyHermiT, and offline tools choose different import policy over the same parsed
root document.

Accepted source forms and ownership:

| Source | Rules | Ownership |
|---|---|---|
| filesystem path | regular file under path policy; not URL | core opens/closes one descriptor |
| `bytes`/readonly buffer | exact bytes; caller may release after return | core copies or fully owns before return |
| `BinaryIO` | must return bytes; document IRI required if no path/base | caller retains/ closes stream |
| `TextIO` | explicit format and document IRI required | code points read once, UTF-8 encoded; caller closes |

A plain `str` is always a path. Ontology source text is never guessed from a
string. For text streams, `source_sha256` is over the UTF-8 encoding of exactly
the returned code points, provenance uses `digest_kind="normalized-text"`, and
no Unicode normalization occurs. Binary path/stream digests are exact bytes.

Readers do not require seekability. Bounded detection buffers are replayed into
the parser. Path parsing opens once and avoids check-then-open races; security
rules are in `security.md`.

The closure facades preserve the same root-source contract. Standalone
consumers pass a stream base explicitly:

```python
snapshot = coerce_snapshot(
    stream,
    document_iri="urn:consumer:root",
    options=LoadOptions(
        format=DocumentFormat.FUNCTIONAL,  # mandatory for TextIO
        imports=ImportPolicy.IGNORE,
    ),
)
```

`document_iri` is a keyword of `load_snapshot`/`coerce_snapshot`, rather than a
field of `LoadOptions`, because it identifies only the acquired root. Resolver
results bind imported documents independently. It is acquisition-only:
documents, views, and providers are already bound and reject the keyword rather
than being copied, reparsed, or rebased. A caller-owned stream is consumed in
one forward pass, is never rewound or retried, and remains open on success or
failure.

## 2. Required formats

### 2.1 RDF/XML and Turtle

These readers parse an RDF graph and apply the normative OWL 2 reverse mapping
to structural objects. They support the complete mapped OWL 2 vocabulary,
axiom/ontology annotations, RDF lists, negative assertions, property chains,
keys, datatype restrictions, and anonymous individuals. SWRL/RDF rule mapping
is an explicitly enabled extension with separate capability/diagnostics.

Parsing triples is not completion. The mapping phase MUST:

- implement W3C canonical parsing and duplicate elimination;
- distinguish entity types and report illegal/ambiguous typing;
- consume/track triples by mapping rule;
- report malformed/shared/cyclic RDF list structures deterministically;
- standardize anonymous individuals within the document scope;
- retain legal extra annotation triples; and
- report every unconsumed logical-looking triple in `RDFMappingReport`.

An RDF graph that cannot be mapped to an OWL 2 structural ontology is not
silently accepted as an empty/partial ontology. Strict mode raises
`UnsupportedSyntaxError` with mapping rule IDs and bounded examples. Explicit
`allow_partial_rdf_mapping=True` returns a document plus a nonconformant report;
no reasoner-facing default enables it.

RDF/XML external entities, DTD processing, and network retrieval are disabled.
Turtle base/prefix expansion follows its Recommendation and configured base.

### 2.2 OWL/XML

The reader implements the OWL 2 XML Serialization grammar and canonical parsing
rules. Namespace processing is standards-compliant; general/external entities,
DTDs, XInclude, schemas fetched from a network, and implementation-specific XML
object deserialization are forbidden.

### 2.3 Functional-Style Syntax

The reader implements the W3C grammar including prefixes, ontology identity,
imports, nested annotations, all expressions/axioms, node IDs, escaped strings,
and, when explicitly enabled, the namespaced SWRL/DL-safe extension.
Tokenization is streaming/bounded, reports Unicode source
spans, and never uses Python `eval` or generated executable code.

### 2.4 Writers

Functional-Style and RDF/XML writers are release requirements. Turtle and
OWL/XML writers are required by 1.0 unless changed in the master spec. A writer
accepts a `RenderOptions` value specifying:

- canonical versus readability-oriented ordering;
- explicit base/prefix map and collision policy;
- root-only, closure, or materialized view scope;
- source annotation/provenance inclusion policy;
- deterministic blank-node labels; and
- an explicit lossy-feature policy (`ERROR` by default).

Canonical writers emit identical bytes for structurally equivalent values
under equal options/backends. Pretty writers need deterministic normalized
output, not source layout preservation. All output is written atomically for
paths and incrementally for streams; partial failures never replace a target.

Parse → render → parse preserves complete structural identity and annotations.
RDF graph comparison uses isomorphism, not blank labels or triple order.

## 3. Format selection

Precedence is:

1. explicit `DocumentFormat`;
2. authoritative media type supplied by an acquisition resolver;
3. bounded content sniffing using documented signatures; then
4. path extension only as a weak hint.

Detection does not “try every parser until one accepts,” which can change error
semantics and magnify hostile-input cost. A mismatch between strong content and
extension emits `FormatGuessWarning`; a mismatch with explicit format is a
syntax error for that format. Ambiguous content raises `FormatDetectionError`
and requests an explicit format.

The recognized extension table and media types are frozen/tested. `.owl` and
`.rdf` do not uniquely identify a concrete syntax and require sniffing.

## 4. Parser diagnostics and recovery

Diagnostics include a stable code, syntax/mapping rule, document IRI, byte and
Unicode line/column span where available, bounded safe excerpt, and import chain
when called by a loader. Paths and credentials obey redaction settings.

Strict parsing stops at a bounded useful error frontier. An editor recovery mode
MAY collect multiple errors but never returns an object advertised as a valid
`OntologyDocument`; it returns a separate `PartialDocument` that
`load_snapshot`/reasoners reject. Recovery is not used in benchmarks or release
conformance tests.

Warnings are deterministic and capped by `max_diagnostics`, with a final
suppression count. Native and Python backends normalize to the same code and
structural location; exact wording may improve without API versioning.

## 5. Provenance and source maps

`DocumentProvenance` records immutable acquisition facts:

```text
source_sha256
digest_kind = exact-bytes | normalized-text
byte_length / decoded_codepoint_length
document_iri and acquisition locator (redactable)
format and detection basis
media type / expected digest if supplied
parser/backend/API/model versions
resolution timestamp only in report, never fingerprint
```

An optional `SourceMap` maps canonical structural values/occurrences to one or
more source spans and retains nonsemantic lexical details needed by tools:
prefix spelling, source blank label, language-tag spelling, redundant
occurrences, and grouping/order trivia. It is bounded and disabled by default
on very large ontology loads. Disabling it cannot affect model identity.

Because structurally duplicate axioms can occur in multiple imported documents,
`OriginIndex` records `(document_key, occurrence, span?)` separately from axiom
identity. Consumers use it for diagnostics, never as semantic identity.

## 6. Import policies

`ImportPolicy` has exact behavior:

| Policy | Resolver calls | Missing import | Network |
|---|---|---|---|
| `IGNORE` | none | manifest status `ignored` | never |
| `RECORD_UNRESOLVED` | configured resolver | warning/status; successful imports included | only if resolver allows and `offline=False` |
| `RESOLVE_LOCAL` | local/catalog/mapping resolvers only | error | never |
| `RESOLVE_STRICT` | all explicitly configured/allowed resolvers | error | only with `offline=False` and allowlists |

Defaults are `RESOLVE_LOCAL` plus `offline=True`; an application can choose
`IGNORE` explicitly for legacy pyELK behavior. pyHermiT requires a snapshot
whose manifest has no ignored/unresolved entries before OWL 2 DL validation.

The policy, offline flag, resolver configuration fingerprint (excluding
credentials), and every resolution outcome are in the import manifest and
snapshot structural fingerprint. They are not in the logical fingerprint.

## 7. Resolver composition

Built-in resolvers are explicit immutable values:

- `MappingResolver`: exact import IRI → source mapping;
- `CatalogResolver`: supported, securely parsed XML/JSON catalog mappings;
- `DirectoryResolver`: allowlisted local root with a declared naming strategy;
- `CompositeResolver`: ordered child resolvers with outcome trace; and
- `HttpResolver`: optional HTTPS/HTTP acquisition with strict allowlists,
  redirects, time/byte limits, integrity, and cache rules.

Resolver ordering is configuration, not plugin installation order. A resolver
returns `ResolvedDocument`; it never parses. The loader owns recursion and
deduplication. Resolver exceptions are normalized and chained; “not found” is a
typed outcome distinct from access denied, timeout, integrity failure, and
malformed source.

Entry-point resolvers must be requested by name. Untrusted documents cannot
select/instantiate plugins or inject resolver configuration.

## 8. Closure algorithm

The loader performs deterministic graph traversal:

1. Parse or accept the root document.
2. Canonically sort its direct import IRIs for scheduling.
3. Resolve each under policy, validate locator/digest/limits, and parse once.
4. Identify a document by resolved canonical document key and exact source
   digest; reconcile ontology/version IRIs under explicit conflict rules.
5. Add import edges and continue until the work queue is empty.
6. Freeze strongly connected components/document scopes and the manifest.
7. Construct the immutable snapshot and fingerprints.

Ordinary OWL import cycles are legal. They are represented as graph cycles and
each canonical document is visited once; they do not raise `ImportCycleError`.
That exception is reserved for resolver/catalog alias cycles, HTTP redirect
cycles, or an explicit resource-policy violation.

Two distinct byte sources claiming the same ontology/version IRI are a
`DocumentIdentityConflictError` unless a mapping policy explicitly pins the
winner and records the rejected source. Silent last-wins behavior is forbidden.
The same exact document reached through aliases is deduplicated but all import
edges/acquisition aliases remain in provenance.

Resolution is reproducible under concurrency: fetches may run in parallel with
bounded workers, but manifest/document ordering, chosen conflict outcome,
diagnostics, and fingerprints follow canonical request order.

## 9. Cache and HTTP semantics

An acquisition cache stores exact bytes plus URL/import IRI, final locator,
digest, media type, validators, retrieval time, and policy metadata. A parsed
document cache keys by source digest, format, parser/model schema, relevant
options, and backend-independent canonical behavior.

Offline mode never performs DNS/socket access and may use only integrity-
verified cached content permitted by policy. HTTP cache revalidation, redirects,
compression expansion limits, TLS verification, proxy behavior, and credential
redaction are explicit. No credentials enter keys, reports, exceptions, or wire
snapshots.

Snapshot/cache atomicity, locking, and wire validation follow `wire-format.md`.

## 10. Conformance tests

Each required format has:

- W3C positive/negative syntax and mapping cases with provenance/license;
- one fixture per constructor including nested annotations;
- cross-syntax structurally equivalent fixture families;
- malformed/truncated/encoding/depth/list/IRI/literal hostile cases;
- blank-node alpha-renaming and RDF graph-isomorphism cases;
- parse/render/parse properties in both backends;
- stream/path/buffer and nonseekable chunk-boundary tests; and
- TextIO explicit-format/document-IRI and normalized digest tests.

Import suites cover missing documents under each policy, catalogs/mappings,
diamonds, legal cycles, ontology/version conflicts, aliases, redirects, offline
caches, integrity failures, concurrent deterministic scheduling, cancellation,
limits, and cleanup of partial cache files.
