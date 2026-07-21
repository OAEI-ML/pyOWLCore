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

Run the ordinary bounded security lanes with:

```console
pytest tests/security tests/fuzz tests/conformance
python -m tools.security.evidence --check
```

The matrix is evidence routing, not a substitute for running its tests. Release
reports must record the exact interpreter, Rust toolchain, native artifact, and
command outcomes.
