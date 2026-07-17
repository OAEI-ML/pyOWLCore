# Native fuzz lanes

These `cargo-fuzz` targets compile the production Rust parser and wire modules
directly. They do not initialize Python, download Java, or replace the ordinary
Python/native differential suite.

Run from the repository root with an installed nightly toolchain and
`cargo-fuzz`:

```console
cargo +nightly fuzz run --manifest-path tests/fuzz/native/Cargo.toml functional \
  tests/data/corpus/w3c/functional -- -max_len=1048576
cargo +nightly fuzz run --manifest-path tests/fuzz/native/Cargo.toml wire \
  tests/data/corpus/hostile -- -max_len=4194304
```

The continuous lane runs each target with AddressSanitizer and Undefined
BehaviorSanitizer, retains minimized failures, and registers any promoted
regression in `tests/data/PROVENANCE.toml`. Reproduce and minimize with
`cargo fuzz tmin`; Python failures use `python -m tools.security.minimize`.

Success requires no panic, abort, sanitizer finding, unbounded allocation, or
accepted corrupt wire—not merely completion for a fixed duration.
