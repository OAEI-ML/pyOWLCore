# Getting started

This guide shows the everyday tasks: loading an ontology, inspecting it,
querying structure, layering changes, converting formats, and sharing one
parsed view across components and processes. Every snippet runs as written
against the pure Python implementation; none of them requires Java, a
compiler, or network access. The [API guide](api.md) describes the complete
surface, and [views and architecture](views-and-architecture.md) defines the
ownership contracts the snippets rely on.

```bash
python -m pip install pyowl-core
```

The distribution is `pyowl-core`; Python code imports `pyowl_core`.

## Load an ontology

`load_snapshot` parses one root source, applies the import policy, and returns
an immutable `OntologySnapshot`:

```python
from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    load_snapshot,
)

SOURCE = b"""\
Ontology(<urn:example:onto>
    Declaration(Class(<urn:example#Animal>))
    Declaration(Class(<urn:example#Dog>))
    SubClassOf(<urn:example#Dog> <urn:example#Animal>)
)
"""

snapshot = load_snapshot(
    SOURCE,
    document_iri="urn:example:doc",
    options=LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    ),
)
assert snapshot.is_complete
```

Input rules that prevent surprises:

- **Bytes are document content.** A `bytes` value is parsed directly.
- **A plain string is a filesystem path**, never ontology text or a URL:
  `load_snapshot("ontologies/doid.owl")` reads that file.
- **Streams require an explicit `format` and `document_iri`.** Caller-owned
  streams are read once and remain open.
- **`format=None` autodetects** among RDF/XML, Turtle, OWL/XML, and Functional
  Syntax. The detected format is recorded on
  `snapshot.root.provenance.format`. Pass an explicit `DocumentFormat` when
  the source format is known.

`LoadOptions` defaults are safe for untrusted input: `imports` defaults to
`ImportPolicy.RESOLVE_LOCAL`, `offline` defaults to `True`, and `limits`
defaults to a bounded `ParseLimits`. Nothing is fetched from the network
unless you configure a resolver that explicitly allows it — see
[security](security.md).

## Parse a single document

`parse_document` parses exactly one source and never resolves imports; direct
imports are recorded, not fetched. Use it for format conversion, single-file
inspection, or when a snapshot closure is not needed:

```python
from pyowl_core import parse_document

document = parse_document(
    SOURCE,
    format=DocumentFormat.FUNCTIONAL,
    document_iri="urn:example:doc",
)
assert document.direct_imports == ()
```

## Resolve imports without the network

Imports resolve only through an explicit resolver under the configured
policy. `MappingResolver` maps exact ontology IRIs to local content:

```python
from pyowl_core import MappingResolver

ROOT = b"""\
Ontology(<urn:example:root>
    Import(<urn:example:child>)
    Declaration(Class(<urn:example#RootClass>))
)
"""
CHILD = b"Ontology(<urn:example:child> Declaration(Class(<urn:example#ChildClass>)))"

closed = load_snapshot(
    ROOT,
    document_iri="urn:example:root-document",
    options=LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.RESOLVE_LOCAL,
        backend=BackendPreference.PYTHON,
        offline=True,
    ),
    resolver=MappingResolver({"urn:example:child": CHILD}),
)
assert closed.is_complete
assert len(closed.documents) == 2
```

`ImportPolicy.IGNORE` skips imports, `RECORD_UNRESOLVED` records them without
failing, `RESOLVE_LOCAL` resolves through the supplied resolver, and
`RESOLVE_STRICT` fails rather than returning an incomplete closure. What
actually happened is recorded on `snapshot.import_manifest` (policy, edges,
per-document status, offline flag). `DirectoryResolver`, `CatalogResolver`,
and `CompositeResolver` cover local layouts; network acquisition requires an
explicitly configured `HttpResolver`. The complete runnable example is
[`examples/secure_local_import.py`](examples/secure_local_import.py).

## Inspect what loaded

```python
axioms = tuple(snapshot.iter_axioms())
assert len(axioms) == 3
assert snapshot.contains(axioms[0])

report = snapshot.report          # LoadReport: counters and timings
diagnostics = snapshot.diagnostics  # bounded tuple of Diagnostic values
```

Model values are immutable, and equality is canonical structural identity: an
axiom parsed from Turtle equals the same axiom parsed from Functional Syntax
or constructed by hand from `pyowl_core.model` constructors.

## Query structure through indexes

`view.view(IndexType, **options)` builds a lazy structural index on first use
and reuses it afterwards. Indexes expose asserted structure only; inferred
taxonomy and realization remain reasoner-owned.

```python
from pyowl_core import (
    IRI,
    AssertedClassHierarchyView,
    Class,
    DeclarationIndex,
    SignatureView,
)

signature = snapshot.view(SignatureView)
class_iris = sorted(str(e.iri) for e in signature.iter())
assert class_iris == ["urn:example#Animal", "urn:example#Dog"]
assert signature.entities_by_iri(IRI("urn:example#Dog"))

declarations = snapshot.view(DeclarationIndex)
assert declarations.is_declared(Class(IRI("urn:example#Animal")))

hierarchy = snapshot.view(AssertedClassHierarchyView)
parents = list(hierarchy.asserted_parents(Class(IRI("urn:example#Dog"))))
assert len(parents) == 1
```

Other public index families cover annotations, axiom types, entity
references, expression occurrences, property hierarchies, domains and ranges,
inverses and property chains, and ontology identities; see
`pyowl_core.index.__all__` and the [API guide](api.md#structural-indexes).

## Change without mutating

Views are immutable. To trial a change, describe it as an `OntologyDelta` and
apply it; the result is a persistent `OntologyOverlay` that keeps the base by
identity instead of copying it:

```python
from pyowl_core import (
    CanonicalSet,
    Declaration,
    OntologyDelta,
    SubClassOf,
    apply_delta,
)

cat = Declaration(Class(IRI("urn:example#Cat")))
cat_is_animal = SubClassOf(Class(IRI("urn:example#Cat")), Class(IRI("urn:example#Animal")))

overlay = apply_delta(
    snapshot,
    OntologyDelta(add_axioms=CanonicalSet((cat, cat_is_animal))),
)
assert overlay.base is snapshot
assert len(tuple(overlay.iter_axioms())) == 5
```

The three fingerprints have separate domains, which matters for cache keys:

```python
# The overlay adds a declaration and a logical axiom, so all three change.
assert overlay.structural_fingerprint != snapshot.structural_fingerprint
assert overlay.signature_fingerprint != snapshot.signature_fingerprint
assert overlay.logical_fingerprint != snapshot.logical_fingerprint

# A declaration-only overlay changes structural and signature fingerprints
# but not the logical fingerprint: declarations are not logical axioms.
decl_only = apply_delta(snapshot, OntologyDelta(add_axioms=CanonicalSet((cat,))))
assert decl_only.logical_fingerprint == snapshot.logical_fingerprint
assert decl_only.structural_fingerprint != snapshot.structural_fingerprint
```

Overlays accept the same index requests as snapshots:
`overlay.view(SignatureView)` reflects the added entities.

## Combine ontologies

`compose_views` builds an `OntologyComposite` that retains each member by
identity — the alignment-trial pattern used by OAEI evaluation:

```python
from pyowl_core import compose_views

target = load_snapshot(
    b"Ontology(<urn:example:target> Declaration(Class(<urn:example#Hund>)))",
    document_iri="urn:example:target-doc",
    options=LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    ),
)

bridge = OntologyDelta(add_axioms=CanonicalSet((
    SubClassOf(Class(IRI("urn:example#Hund")), Class(IRI("urn:example#Dog"))),
)))
trial = compose_views(snapshot, target, delta=bridge, roles=("source", "target"))
assert tuple(member.view for member in trial.members) == (snapshot, target)
```

To vary only the bridge between trials, call `compose_views` again with a new
delta; the members are reused by identity and nothing is reparsed.

## Convert between formats

`render_document` deterministically renders one parsed document:

```python
from pyowl_core import render_document

turtle_bytes = render_document(document, format=DocumentFormat.TURTLE)
```

`write_document` writes to a path or stream, and `RenderOptions` controls
prefixes and lossy-rendering policy. Rendering operates on an
`OntologyDocument`; a snapshot exposes its parsed documents as
`snapshot.documents`, with the root at `snapshot.root`.

## Hand one view to several consumers

In-process handoff is by identity, not serialization. `coerce_snapshot`
returns an existing view unchanged and parses only acquisition inputs
(bytes, paths, streams):

```python
from pyowl_core import coerce_snapshot

assert coerce_snapshot(snapshot) is snapshot
```

A provider object implements `owl_snapshot()` and returns the exact stored
view; `coerce_snapshot(provider)` validates and returns that same object.
Passing `format`, `resolver`, or a root `document_iri` alongside an existing
view raises `OptionConflictError` instead of reparsing. See
[consumer handoff](consumer-handoff.md) and
[`examples/parse_once.py`](examples/parse_once.py).

## Share across processes

Cross-process and cache transport uses the validated wire format — never
pickle:

```python
from pyowl_core import decode_snapshot, encode_snapshot

payload = encode_snapshot(snapshot)          # bytes
received = decode_snapshot(payload)          # structurally equal, distinct view
assert received.logical_fingerprint == snapshot.logical_fingerprint
```

For durable files and near-zero-copy reopening:

```python
from pyowl_core import open_snapshot, write_snapshot

write_snapshot(snapshot, "onto.pyowlwire")            # atomic by default
reopened = open_snapshot("onto.pyowlwire")            # mmap-backed by default
```

`WireCache` manages a directory of versioned entries keyed by structural
fingerprint:

```python
from pyowl_core import WireCache

cache = WireCache("wire-cache")
with cache.get_or_publish(snapshot) as mapped:   # publishes once, then reopens
    assert mapped.logical_fingerprint == snapshot.logical_fingerprint
```

Decoders validate lengths, counts, and checksums before allocation and fail
closed on unknown required features or incompatible schemas. Wire images are
for transport and caching; in-process consumers should keep sharing the
original object.

## Handle failures

Every public failure derives from `PyOWLCoreError`, and every warning from
`PyOWLCoreWarning`. Branch on exception type and stable diagnostic code,
never on message text:

```python
from pyowl_core import OntologySyntaxError, ParseError, PyOWLCoreError

try:
    parse_document(b"Ontology(", format=DocumentFormat.FUNCTIONAL,
                   document_iri="urn:example:bad")
except OntologySyntaxError as error:
    diagnostic = error.as_diagnostic()
    assert diagnostic.code == "FUNCTIONAL_SYNTAX"
    assert diagnostic.severity.name == "ERROR"
```

`Diagnostic` carries a stable severity, code, and message plus optional source
spans and structured details. Common families include `ParseError` subtypes
for syntax and format detection, `ImportResolutionError` and
`UnresolvedImportError` for closures, `ResourceLimitError` for limits,
`OptionConflictError` for illegal option/view combinations, and `WireError`
subtypes for transport; the full list is in `pyowl_core.exceptions`.

## Bound untrusted input

`ParseLimits` bounds every attacker-controllable dimension before allocation.
Named limit failures expose uniform structured fields:

```python
from pyowl_core import ParseLimits, ResourceLimitError

try:
    load_snapshot(
        SOURCE,
        document_iri="urn:example:doc",
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            limits=ParseLimits(max_axioms=1),
        ),
    )
except ResourceLimitError as error:
    # Parsing stops at the first axiom over the limit.
    assert (error.limit, error.observed, error.allowed) == ("max_axioms", 2, 1)
```

Long-running loads accept a `cancellation_token`; create one with
`CancellationSource`. Deadlines and memory bounds are `ParseLimits` fields
(`deadline_seconds`, `max_memory_bytes`). See [security](security.md) for the
complete untrusted-input guidance.

## Choose a backend

- `BackendPreference.PYTHON` — the complete portable implementation,
  explicit and silent. Every snippet above uses it.
- `BackendPreference.AUTO` (default) — selects the private native accelerator
  when a compatible wheel is installed, otherwise the Python path with a
  one-time `NativeBackendUnavailableWarning`.
- `BackendPreference.NATIVE` — requires acceleration and raises instead of
  falling back.

Public values and behavior are identical across backends; no public value is
a PyO3/Rust object. See [troubleshooting](troubleshooting.md) when the
native wheel is not selected.

## Where next

- [API guide](api.md) — the complete reviewed surface and version tuples.
- [Views and architecture](views-and-architecture.md) — ownership and
  lifecycle contracts for documents, snapshots, overlays, and composites.
- [Consumer handoff](consumer-handoff.md) — reasoner, projector, and
  evaluation integration patterns.
- [Security](security.md) — resolvers, plugins, and cache trust boundaries.
- [Compatibility](compatibility.md) — version domains and tested consumers.
