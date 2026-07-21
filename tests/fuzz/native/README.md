# Native fuzz lanes

These `cargo-fuzz` targets compile the production Rust parser and wire modules
directly. They do not initialize Python, download Java, or replace the ordinary
Python/native differential suite.

Run from `native/` with the pinned nightly toolchain and `cargo-fuzz` version
used by `.github/workflows/native-safety.yml`. Copy seeds to a disposable
corpus directory first because libFuzzer expands the supplied corpus in place:

```console
cargo +nightly-2026-07-14 fuzz run --fuzz-dir ../tests/fuzz/native \
  --sanitizer address functional /tmp/pyowl-core-functional-corpus -- \
  -max_total_time=60 -max_len=1048576 -timeout=10 -rss_limit_mb=2048
cargo +nightly-2026-07-14 fuzz run --fuzz-dir ../tests/fuzz/native \
  --sanitizer address wire /tmp/pyowl-core-wire-corpus -- \
  -max_total_time=60 -max_len=4194304 -timeout=10 -rss_limit_mb=2048
```

The continuous lane runs each target with AddressSanitizer, retains failures,
and registers any promoted minimized regression in `tests/data/PROVENANCE.toml`.
ThreadSanitizer runs the native library against a sanitized standard library;
the dependency-free Miri harness covers the pure canonical and retained-owner
slice where the CPython FFI is not applicable. Reproduce and minimize with
`cargo fuzz tmin`; Python failures use `python -m tools.security.minimize`.

Success requires no panic, abort, sanitizer finding, unbounded allocation, or
accepted corrupt wire—not merely completion for a fixed duration.
