# PYOCORE snapshot wire/cache format

## 1. Purpose and guarantees

Wire v1 is the only stable cross-process representation of an
`OntologyView`. It supports content-addressed caches, memory mapping, IPC, and
cross-language readers without importing Python classes. It is deterministic,
read-only, bounds-checkable, and backend-neutral.

It is not pickle, a Rust serialization crate layout, a dump of object memory,
an RDF syntax, or consumer reasoner IR. No pointer, `usize`, native enum,
padding-dependent struct, Python hash, object address, or filesystem descriptor
appears in it.

Public operations are `encode_snapshot`, `decode_snapshot`, `open_snapshot`,
and `write_snapshot`. Encoding an overlay/composite writes a self-contained
effective snapshot with origin/member metadata; decoding need not reconstruct
the edit tree.

## 2. Version model

`WIRE_FORMAT_VERSION = (major, minor)` is independent of package/API and
`MODEL_SCHEMA_VERSION`.

- Major changes may alter required sections/encoding and are incompatible.
- Minor changes may only add optional skippable sections, new optional flags,
  or relax a constraint without changing existing bytes' meaning.
- A reader accepts its major and any minor whose required features it knows.
- Unknown required section/flag/schema raises `WireVersionError`.
- Unknown optional sections are integrity/bounds checked and skipped.
- Model schema mismatch raises `WireVersionError`; caches rebuild rather than
  attempting semantic reinterpretation.

The implementation keeps golden readers for all supported v1 minors through
the package 1.x line. Migration is decode-old/re-encode-new; in-place mutation
of cache files is forbidden.

The current supported maximum is wire minor 1. `CoreCapabilities.wire_format`
and `WIRE_FORMAT_VERSION` report `(1, 1)` for direct, decoded, and mapped views.
The canonical writer emits minor 1 so every new artifact can carry the mapped
encoded-structural section. Readers continue to accept historical minor-0
images and use the complete scalar fallback when that optional section is
absent.

The `0.2.0` successor raises the supported/emitted maximum to minor 2. It keeps
all required v1 section layouts and adds optional `ENCODED_STRUCTURAL_V2`
(kind `0x8004`, schema 2) for model-schema-2/canonical-model-v2 columns.
`ENCODED_STRUCTURAL_V1` remains kind `0x8003`, schema 1, and model schema 1;
neither its descriptor nor its interpretation changes. A minor-2 reader may
skip an unknown optional encoded section and use scalar required sections, but
the independent model-schema check still rejects a snapshot from an unsupported
model identity line.

## 3. Scalar conventions

- byte order: little-endian;
- integers: exact-width unsigned unless section schema explicitly says signed;
- booleans: `u8` 0 or 1 only;
- IDs: `u32`, zero reserved for “none,” valid IDs start at 1;
- byte offsets/lengths/count metadata: `u64` with checked arithmetic;
- strings: length-delimited valid UTF-8, no NUL convention/normalization;
- digests: raw 32-byte SHA-256;
- alignment: section starts on 8-byte boundaries; padding is zero and checked;
- enums/constructor tags: frozen `u16`/`u32` schema ledger values.

Wire v1 therefore supports fewer than 2^32 entries in each ID table. The
encoder raises `ResourceLimitError(code="WIRE_ID_SPACE")` before overflow. A
future u64-ID format is a wire-major change.

## 4. Fixed header

The file starts with this exact 96-byte header:

| Offset | Width | Field |
|---:|---:|---|
| 0 | 8 | ASCII magic `PYOCORE\0` |
| 8 | 2 | wire major |
| 10 | 2 | wire minor |
| 12 | 4 | header length (96 in v1) |
| 16 | 4 | file feature flags |
| 20 | 4 | section count |
| 24 | 4 | model schema version |
| 28 | 4 | canonical encoding profile (1) |
| 32 | 8 | total file length |
| 40 | 8 | section-directory offset |
| 48 | 8 | section-directory byte length |
| 56 | 32 | file content SHA-256 |
| 88 | 4 | CRC32C of header with digest/CRC fields zeroed |
| 92 | 4 | reserved zero |

The file digest is SHA-256 over the entire file with bytes 56–91 zeroed. It is
checked after cheap header/directory/size validation and before publishing a
snapshot. `verify=False` may skip only this full digest for an authenticated
local cache; section bounds, references, enum values, UTF-8, and required
section checks are never skipped.

Nonzero reserved fields or unknown required file flags are version errors.

## 5. Section directory

The directory has `section_count` fixed 72-byte entries in ascending section
kind, then section-specific data. Each entry is:

| Width | Field |
|---:|---|
| 2 | section kind |
| 2 | section flags (`REQUIRED=1`, `OPTIONAL=2`; exactly one) |
| 4 | section schema version |
| 8 | section offset |
| 8 | stored byte length |
| 8 | decoded byte length (equal in v1) |
| 8 | logical row/item count |
| 32 | SHA-256 of stored section bytes |

Required sections occur exactly once. Sections cannot overlap the header,
directory, each other, or exceed total length. Offset+length uses checked `u64`
arithmetic. Count is validated against minimal row size and caller limits before
allocation. Directory entries are canonical and duplicate kinds fail.

Wire v1 required sections are uncompressed to allow memory mapping and a
compiler-free decoder. Required-section compression or encryption is a future
major/required-feature change. Transport/cache layers may compress the complete
file externally but must restore/validate exact bytes before decoding.

## 6. Required section inventory

The generated `wire-v1-schema.toml` committed by WP06 assigns immutable tags and
row layouts. At minimum v1 contains, in dependency order:

1. `STRINGS`: canonical unique UTF-8 strings sorted by bytes;
2. `IRIS`: string IDs plus validation flags;
3. `ENTITIES`: kind tag and IRI ID;
4. `LITERALS`: lexical string ID, datatype entity ID, language string ID/zero;
5. `ANONYMOUS`: document-scope digest and alpha-canonical local key;
6. `SEQUENCES`: typed ordered/unordered ID vectors with offsets;
7. `ANNOTATIONS`: property/value/annotation-set references;
8. `TERMS`: OWL data-range/class/property constructor rows;
9. `AXIOMS`: constructor, axiom-annotation set, and typed field references;
10. `DOCUMENTS`: IDs, document keys, direct imports, annotation/axiom postings,
    document/source fingerprints and safe provenance flags;
11. `IMPORTS`: policy, resolver-config fingerprint, records/edges/status;
12. `VIEW`: root, effective postings, model/fingerprint/capability metadata;
13. `ORIGINS`: structural values to document/occurrence references; and
14. `FOOTER`: redundant required-section counts/digests and canonical snapshot
    structural/logical/signature fingerprints.

SWRL and other registered extension components use optional, explicitly
namespaced sections with their own required capability/schema; they are never
smuggled into the OWL 2 axiom/term tag space.

Optional v1 sections may include bounded source maps, diagnostics/load reports,
prefix suggestions, and composition role provenance. Machine-local absolute
paths, credentials, bearer/cookie/proxy data, object IDs, timestamps that affect
reproducibility, and native caches are forbidden.

Wire minor 1 defines optional section `VIEW_PROVENANCE` (kind `0x8002`, schema
1). It contains one row: import-manifest SHA-256, loader-diagnostics SHA-256,
`u64` document count, then canonical document identities sorted by UTF-8 key.
Each identity is `u64 key_length + key_utf8`, followed by ontology and version
IRI optionals encoded as `u8 present` and, when present,
`u64 iri_length + iri_utf8`. A version IRI requires an ontology IRI. Counts,
UTF-8, IRI validity, strict key order, and limits are validated before the
metadata is published.

Wire minor 1 also defines optional section `ENCODED_STRUCTURAL_V1` (kind
`0x8003`, schema 1). It contains exactly one closure row for the frozen
`pyowl-core/structural-columns` schema. The row begins with the eight-byte
magic `PYOCEV1\0`, `u16` encoded/model schema versions, `u32` buffer count,
the 32-byte descriptor digest, and a 32-byte digest binding the canonical
encoded roots to the required `VIEW` postings. Eleven `(u64 offset, u64
length)` entries follow in descriptor buffer order. Buffer starts are
eight-byte aligned relative to the row, gaps are zero, and the final slice
ends exactly at the row boundary.

Readers validate the descriptor, widths, graph structure, canonical ordering,
resource limits, and root binding before publication. A mapped closure request
borrows the eleven read-only slices from one mapping exporter and retains a
snapshot lease. Historical files and non-closure selections use the complete
scalar fallback rather than manufacturing a zero-copy claim.

Wire minor 2 defines `ENCODED_STRUCTURAL_V2` (kind `0x8004`, schema 2) with the
same bounded directory/container rules but the generated
`pyowl-core/structural-columns` schema-2 descriptor, model schema 2, and magic
`PYOCEV2\0`. Its canonical root binding is computed over canonical-model-v2
bytes. A model-schema-2 writer MUST NOT emit the v1 section, and a reader MUST
NOT reinterpret either section through the other descriptor.

The section is emitted only when required-section metadata would lose the
source view's loader-diagnostic digest or pre-materialization overlay/composite
document identities. Minor-0 images derive identities and the exact canonical
manifest digest from `DOCUMENTS`/`IMPORTS`, and use the canonical empty loader
diagnostic digest. Decoded and mmap views therefore expose equal identity
metadata without retaining full diagnostics or materializing model roots.

## 7. Table and reference rules

Rows use section-specific fixed headers plus offset/length slices into an
adjacent payload. Every slice is aligned, in-section, nonoverlapping where
required, and exactly covers its declared item type. Zero references are legal
only for optional fields.

Dictionary/table order is canonical by complete canonical model bytes, not
insertion order. DAG references point to lower canonical/topological layers or
are validated with a cycle-safe graph pass. Cycles are allowed in the import
graph, never in recursive structural terms/annotations.

Unordered sets contain strictly ascending unique IDs under canonical target
order. Ordered sequences retain order/repetition. Constructor arity, field
types, entity kinds, language/datatype constraints, annotation recursion,
anonymous document scopes, and axiom categories are validated while decoding.
A syntactically bounds-valid but structurally invalid file is corruption.

The decoder never instantiates a Python object for each row merely to validate
the file. It validates columns/buffers in bulk and constructs lazy handles or
fallback objects only on access, while preserving complete Python semantics.

## 8. Canonical encoding algorithm

1. Freeze/validate the effective `OntologyView`.
2. Compute canonical structural bytes and blank-node labels independently of
   backend/insertion order.
3. Collect/intern values and sort dictionaries/tables canonically.
4. Assign dense IDs in sorted order.
5. Emit required sections, then approved optional sections in kind order.
6. Emit the directory, zero padding, and provisional header.
7. Compute section digests, footer fingerprints, header CRC, and full digest.
8. Return immutable bytes or atomically publish a file.

The Python and Rust encoders must produce byte-identical v1 files. An
independent minimal encoder/decoder validates golden fixtures so shared bugs in
the production implementations are detectable.

Canonical encoding of two effective views with equal complete structure is
byte-identical regardless of source syntax/backend/path/import scheduling. An
overlay/composite may include different optional edit/role provenance only when
the caller opts into that optional section; the canonical default omits it.

## 9. Decoder validation order

To prevent hostile allocation/work amplification, decoding proceeds:

1. require at least 96 bytes; check magic/version/header length/reserved fields;
2. check total length against actual source and limits;
3. bounds-check directory size/count with checked arithmetic;
4. read directory and validate kinds, uniqueness, flags, bounds, overlaps,
   alignment, minimal sizes, and claimed counts;
5. validate required-section presence/schema/capabilities;
6. verify small header/directory and per-section digests in bounded chunks;
7. validate string bytes/UTF-8 and scalar columns;
8. validate references, slices, sorting/uniqueness, arities, DAGs/import graph;
9. recompute canonical fingerprints and optionally the complete file digest;
10. publish an immutable snapshot only after all checks pass.

No claimed count causes allocation before steps 1–5. Deadline/cancellation and
memory accounting are checked between bounded chunks. Error diagnostics reveal
offset/section/code but not arbitrary hostile bytes.

## 10. Memory mapping and ownership

`open_snapshot(path, mmap=True)` opens one validated immutable mapping. It
detects replacement/truncation during validation and retains the descriptor or
platform-equivalent stable handle. Writers never modify a published file in
place, so readers see old or new complete files.

The mapped snapshot owns the mapping; dependent terms/views/overlays/composites
keep it alive. `close` follows `snapshots-overlays.md`. A caller never receives a
writable memoryview. On architectures where unaligned loads are unsafe, the
decoder reads scalars explicitly; it never casts unchecked packed structs.

For IPC, file descriptors/shared memory can transport the exact wire file with
an authenticated expected length/digest. Pointer/capsule sharing is forbidden.

## 11. Atomic cache publication

`write_snapshot(..., atomic=True)`:

- writes a unique same-directory temporary file with restrictive permissions;
- flushes and optionally fsyncs file/directory under `DurabilityPolicy`;
- validates the completed file with the independent reader;
- atomically replaces/links the content-addressed target; and
- cleans temporary files on errors/cancellation.

Concurrent writers use content-addressed final names and a bounded advisory
lock protocol containing PID/start/token, with stale-lock handling that cannot
delete another live writer's lock. Equal content converges; differing content
never shares a final digest path. Readers never trust a sidecar without the
file's own validation.

Cache path template includes wire major, model schema, and structural digest.
Cache GC treats active mappings/leases safely and never follows symlinks outside
its configured root.

## 12. Errors and recovery

Explicit IPC decode raises typed `WireVersionError`, `WireCorruptionError`, or
`WireLimitError`. A cache facade may catch known version/corruption failures,
quarantine/delete only the verified in-root cache entry, and rebuild from an
authorized source. It records the event; it never silently returns partial data.

There is no best-effort recovery of a corrupt snapshot. Optional sections can
be skipped only when their flag/schema says so and their bounds/integrity pass.

## 13. Acceptance and fuzz gates

- byte-level golden fixtures for empty, every constructor, imports/cycles,
  annotations, anonymous symmetry, overlays, and compositions;
- Python ↔ Rust ↔ independent reader/encoder cross-product;
- deterministic bytes across Python hash seeds, threads, source formats, and
  insertion permutations;
- every byte/field/length/count/offset/tag/reference corruption family;
- truncation at every byte for a compact fixture;
- AFL/libFuzzer and Python property fuzzers with no panic/abort/OOM/hang;
- 32/64-bit arithmetic model tests even if wheels are 64-bit;
- mmap replace/truncate/close/fork/concurrent writer cases;
- cache crash-injection at each publication step;
- version/unknown optional/unknown required/migration golden tests; and
- large biomedical snapshots prove mmap startup avoids full object expansion
  and stays within performance/memory gates.
