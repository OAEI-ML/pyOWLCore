# Native pipeline capabilities

These additive interfaces preserve existing schema-2 structural fingerprints and
ordinary package defaults. Load retained snapshots with
`LoadOptions(backend=BackendPreference.NATIVE)`.

## Validated structural publications

```python
view = owner.view(EncodedStructuralViewV2, require_native_validation=True)
view = validate_encoded_structural_view_v2(
    view, expected_owner=owner, expected_scope=AxiomScope.CLOSURE,
    expected_document_key=None, require_native_validation=True,
)
report = native_validation_report(view)
```

`native_validation_available()` probes the actual loaded binary capability without
constructing an ontology, allowing consumers to reject mixed or old installations
early. A positive probe never substitutes for receipt admission.

Admission checks a native-issued receipt bound to the retained owner/storage,
original immutable buffers, scope and construction budgets. The checked native
builder supplies the structural validation; the schema fingerprint is computed
once with bulk hashing and retained in the receipt. Replaced buffers, foreign
owners, changed metadata and released memoryviews are rejected. Changed structural
budgets trigger checked native construction under those budgets. Closing the owner
prevents further admission; retained immutable buffer bytes keep their lifetime.

This capability currently supports direct retained native snapshots, including
imported selections. Strict requests for Python, decoded, mapped, composite or
overlay owners raise `BackendProtocolError` before scalar production. The report is
diagnostic; it cannot replace receipt verification.

`encoded_scopes_equivalent(owner, left_scope, right_scope, *,
left_document_key=None, right_document_key=None)` proves equality of the selected
native structural root tables only for a direct single-document snapshot without
import edges. It does not publish columns. False means no proof; consumers must use
their normal native path or reject unsupported capabilities. Equality must not be
inferred from document count alone.

## Filtered annotation pages

```python
assert AnnotationAssertionIndex.supports_native_columns(owner)
index = owner.view(AnnotationAssertionIndex, include_origins=False,
                   require_native_pipeline=True)
for page in index.iter_columns(properties=[AnnotationProperty(IRI(property_iri))],
                               max_rows=1024, max_bytes=8 * 1024 * 1024):
    for subject, value in zip(page.subjects, page.values):
        consume(subject, value)
```

The native index retains constructor-selected root IDs and filters before Python
wraps the requested scalar results. Each immutable `AnnotationAssertionColumns`
page contains `subjects`, `properties`, `values`, `canonical_assertion_bytes`,
`assertion_digests`, `origins`, and an actual-operation `report`. Rows follow the
selected ontology's canonical assertion order. Nested annotations remain in the
complete assertion bytes; equal triples with different annotations remain distinct.
IRI/anonymous identity, literal lexical form, datatype and language are preserved.
Origins, if enabled, are looked up only for emitted assertion digests.

`None` filters select all; empty filters select none. `max_bytes` bounds the sum of
the five encoded payload columns, including each digest. An indivisible oversized
row raises `ResourceLimitError`; it is never omitted or truncated. Native memory
limits additionally account for retained and temporary storage. Cancellation is
checked natively. Published pages remain readable after source closure, while new
requests follow the owner's close contract.

Strict pages support direct retained snapshots and `include_nested=False` only.
`iter_subject`, `assertions`, `values` and literal selection can use the selected
native pages. Strict subject/reverse enumeration and nested occurrence queries are
not implemented and fail explicitly. Ordinary index behavior is unchanged, except
that constructing an index without nested results no longer traverses unrelated
roots. Application-specific exclusion rules remain in the application.
