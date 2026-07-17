# WP09 security evidence

`control-matrix.json` maps each reviewed threat to concrete executable tests.
The matrix is deterministic and checked in both Python 3.10 and 3.12 lanes.
The native fuzz targets and sanitizer procedure are documented in
`tests/fuzz/native/README.md`; retained hostile inputs are hash-pinned by the
conformance corpus lock.

Run the ordinary bounded security lanes with:

```console
pytest tests/security tests/fuzz tests/conformance
python -m tools.security.evidence --check
```

The matrix is evidence routing, not a substitute for running its tests. Release
reports must record the exact interpreter, Rust toolchain, native artifact, and
command outcomes.
