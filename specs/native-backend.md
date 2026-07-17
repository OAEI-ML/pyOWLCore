# Rust acceleration and complete Python fallback

## 1. Architecture decision

The optimized backend is a private Rust extension built with PyO3 (or a later
reviewed equivalent). It accelerates coarse parser, canonicalization, indexing,
and wire operations. It is not the public model and is never required for
semantic completeness.

```text
pyowl_core public API/model
        |
pyowl_core.backends.dispatch
       / \
 complete Python   private pyowl_core._native
```

All public values/reports/errors are core Python contracts. No PyO3 class,
Horned-OWL type, raw buffer owner, pointer, or Rust enum appears in a stable
signature. Python and native values compare/interoperate without a format or
semantic fork.

## 2. Candidate Rust workspace

```text
native/
  Cargo.toml                 # cdylib; exact MSRV and lockfile
  src/
    lib.rs                   # narrow PyO3 boundary and panic containment
    error.rs
    limits.rs
    cancel.rs
    source.rs
    parse/
      rdf.rs
      owlxml.rs
      functional.rs
    map_rdf.rs
    canonical.rs
    model/                   # private structural arena matching schema
    index/
    wire/
    session.rs
```

Parsing/model code has no PyO3 dependency; `lib.rs` converts validated inputs
and results. Rust dependencies are minimal, pinned in `Cargo.lock`, audited for
license/advisories/MSRV, and compiled with reproducible feature sets.

Horned-OWL may be used only after a recorded capability/conformance/performance
spike and release-blocking legal review of its LGPL/transitive obligations.
Acceptable outcomes, in order of preference, are (a) a clean-room
Apache-compatible core implementation keeping the artifact wholly Apache-2.0,
or (b) correctly multi-licensed artifacts with all required notices, source,
relinking, and user documentation. Project Apache metadata must not conceal
linked third-party terms. `py-horned-owl` is not a runtime bridge dependency
merely to expose its Python model.

## 3. Coarse private API

The authoritative `src/pyowl_core/_native.pyi` exposes only private functions
similar to:

```python
ABI_VERSION: int
MODEL_SCHEMA_VERSION: int
WIRE_FORMAT_VERSION: tuple[int, int]
FEATURES: tuple[str, ...]

def self_test() -> None: ...
def parse_document(source: ReadOnlyBuffer, config: bytes, cancel: object) -> bytes: ...
def build_snapshot(documents: Sequence[ReadOnlyBuffer], config: bytes, cancel: object) -> bytes: ...
def build_index(snapshot_wire: ReadOnlyBuffer, request: bytes, cancel: object) -> bytes: ...
def validate_wire(snapshot_wire: ReadOnlyBuffer, config: bytes) -> bytes: ...
```

Results are validated core wire/mini-wire buffers or bounded diagnostic values,
not millions of nested per-node Python calls. Exact signatures are frozen in
WP07 after benchmarks. Resolver callbacks stay in Python; native parsing never
opens arbitrary imports/network paths.

The extension takes owned data or retains a Python buffer only during the call
under the correct buffer/GIL lifetime. It never keeps a borrowed pointer for a
later session. Read-only mmap ownership stays with the core snapshot.

## 4. Dispatch and fallback

`BackendPreference` behavior is mandatory:

| Request | Native available/self-test/capability | Result |
|---|---|---|
| `PYTHON` | any | Python, no fallback warning |
| `NATIVE` | yes | native |
| `NATIVE` | no/incompatible | `BackendUnavailableError` |
| `AUTO` | yes | native |
| `AUTO` | no/incompatible | Python + once/process warning |

The warning category is `NativeBackendUnavailableWarning`; it states the
operation selected Python, the sanitized reason, and remediation. It is emitted
once per process per unavailability reason, not per axiom/import. Explicit
Python selection is silent. Applications may filter the warning through normal
Python warning controls, never a core global mute.

Capability selection occurs before an operation. Native cannot parse half a
document and hand unsupported constructs to Python. Until a native feature has
full parity, `AUTO` chooses Python for the whole operation and forced native
raises a capability error.

The Python backend cannot import/call native or Horned-OWL. Installing the pure
wheel therefore provides every model/format/import/index/wire feature, albeit
with a performance warning only when `AUTO` attempts unavailable acceleration.

## 5. GIL, threading, and callbacks

Long Rust-only parsing, canonicalization, hashing, and index loops release the
GIL after owning/validating all Python memory. While released Rust:

- calls no Python/resolver/progress callback and creates no Python exception;
- polls cancellation/deadline/resource atomics at bounded strides;
- stores bounded progress/diagnostic events in a native queue;
- uses checked memory accounting and worker limits; and
- holds no mutable alias accessible from Python.

After reacquiring the GIL, errors/events convert once and callbacks execute on
the initiating thread outside internal locks. Callback failure cancels work and
re-raises the original exception after cleanup. `KeyboardInterrupt` is checked
during bounded reattachment/poll points and is not wrapped.

Internal parallelism is explicit and bounded. Default deterministic mode fixes
schedule-independent results and avoids oversubscription when applications run
multiple loads. Thread count and task granularity are benchmarked options.

## 6. Memory and data structures

Preferred internal techniques, only where measured:

- byte-slice/stream parsing with bounded token buffers;
- string/IRI interning and dense typed arenas;
- struct-of-arrays tables aligned with wire/index scans;
- compact canonical set/vector encodings;
- deterministic sorted/radix/hash structures whose iteration cannot leak;
- streaming SHA-256 and external/bounded sort for extreme workloads;
- zero-copy mmap views for stable wire sections; and
- delta posting adapters rather than base copies.

All arithmetic uses checked operations. Allocations reserve against
`max_memory_bytes` before growth. Process OOM/abort is not a resource strategy.
Small-source overhead and Python object materialization are benchmarked as well
as huge ontology throughput.

Rust `unsafe` is denied by default. A measured exception requires documented
aliasing/lifetime/alignment invariants, a safe comparison, dedicated Miri/
sanitizer/fuzz tests, and focused review. Unsafe is never used merely to bypass
PyO3/buffer ownership requirements.

## 7. Panic/error policy

Every FFI entry catches unwinding before it can cross CPython. Release builds do
not use `panic=abort`. A panic returns
`BackendProtocolError(code="NATIVE_PANIC")`, invalidates any affected session,
and is release-blocking. Panic containment is not normal error handling.

Rust internal errors carry stable code/context and convert at one boundary to
public exceptions. `MemoryError` is raised only for actual Python allocation
failure; configured limits use `ResourceLimitError`. Sensitive source snippets,
paths, and resolver data are redacted before crossing.

## 8. Build modes and artifacts

One sdist supports:

| `PYOWL_CORE_BUILD_NATIVE` | Behavior |
|---|---|
| `auto` (default local source build) | attempt optional extension; install complete Python if compilation unavailable |
| `0` | omit extension; deterministic `py3-none-any` wheel |
| `1` | require extension; fail build if unavailable |

Use `setuptools.build_meta` plus `setuptools-rust` or an equivalently proven
optional-extension backend; source metadata stays in `pyproject.toml`. Invalid
values fail. Official native-wheel CI always uses required mode and verifies the
extension, so optional build failure can never produce a mislabeled official
native wheel.

Publish a universal pure-Python wheel, platform CPython native wheels covering
every supported Python starting at 3.10, and one complete sdist. Version-specific
wheels are the safe initial policy. `abi3` is adopted only after PyO3/buffer/
free-threading/subinterpreter compatibility and resolver selection are proven;
wheel tags must never overclaim compatibility. PyPy uses the pure wheel until a
separate native contract passes.

## 9. Parity and rollout

Native work lands feature-by-feature only after Python semantics/goldens freeze:

1. extension self-test/errors and empty wire;
2. strings/IRIs/literals/canonical primitives;
3. one format at a time plus structural mapping;
4. anonymous canonicalization/fingerprints;
5. document/snapshot/import assembly;
6. wire encode/decode/mmap;
7. built-in indexes;
8. overlay/composite posting merges; and
9. controlled parallel/performance work.

For each stage, forced native runs identical golden, generative, hostile, and
consumer suites. `AUTO` advertises only complete capability sets. There is no
“native calls Python for missing semantic cases” release mode.

## 10. Native release gates

- exact extension/API/model/wire version self-test at import;
- Python/native canonical bytes, fingerprints, diagnostics, and wire parity;
- required format/W3C suites and every constructor;
- Miri where applicable, address/thread/undefined sanitizers, fuzz, panic and
  allocation-failure injection;
- GIL release/callback/signal/cancellation/fork/subinterpreter/free-threading
  matrix with unsupported modes selecting Python safely;
- wheel audit on each platform/Python, clean environment install, symbols and
  dynamic library dependency scan;
- Java archive/class/runtime scan and SBOM/license/source obligations;
- compiler-free pure wheel full-suite install; and
- performance gates measured without weakening deterministic/resource modes.

