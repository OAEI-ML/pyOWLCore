# WP09 security evidence

`control-matrix.json` maps each reviewed threat to concrete executable tests.
The matrix is deterministic and checked in both Python 3.10 and 3.12 lanes.
The native fuzz targets and sanitizer procedure are documented in
`tests/fuzz/native/README.md`; retained hostile inputs are hash-pinned by the
conformance corpus lock.

`native-safety-checkpoint.json` records the exact local AddressSanitizer,
ThreadSanitizer, Miri, and bounded libFuzzer results for safety commit
`43e478591dc7bc3e3d66c22c5a36fec595975422`. It deliberately distinguishes
those passing local runs from the committed-but-not-run hosted workflow and
keeps the aggregate release gate at `not-run`.

`native-lifecycle-checkpoint.json` separately binds the local owning-extension
teardown/fork/signal/thread results and repeated CPython subinterpreter fallback
probe. It records unsupported or unavailable interpreter lanes as `not-run`
and the hosted 3.10/3.12/3.14/3.14t matrix as `configured-not-run`.

`native-view-lifecycle-checkpoint.json` binds an exact test-hook artifact to
direct and mmap encoded-buffer concurrency, close, and real-fork coverage. The
local slice passes, while installed-wheel, allocation-failure, and supported-
platform lifecycle evidence remain open and the capability stays hidden.

`native-allocation-checkpoint.json` records an exact, dynamic failpoint sweep
through retained-component build, freeze, and encode for all 76 model
constructors, plus the five explicit native wire validation/receipt allocation
boundaries and 57 positive retained/temporary claims in the validated V2
publication builder, plus 51 explicit backing-owner/slice/dictionary/counter
publication checkpoints in the direct encoded-view Python bridge and 13 actual
capacity-growth/owner checkpoints in its retained Rust encoded-column
workspace, plus 38 positive Functional parser session allocation-budget
claims and 13 explicit Functional parser configuration/source/result Python-
bridge checkpoints, plus 13 native index request/source/result Python-bridge
checkpoints and 39 canonical validation, wire validation, and wire roundtrip
Python-bridge checkpoints. It covers 20,902 checkpoints locally; the remaining
bridge, process-allocator, and hosted-platform matrix stays open.

Run the ordinary bounded security lanes with:

```console
pytest tests/security tests/fuzz tests/conformance
python -m tools.security.evidence --check
```

The matrix is evidence routing, not a substitute for running its tests. Release
reports must record the exact interpreter, Rust toolchain, native artifact, and
command outcomes.
