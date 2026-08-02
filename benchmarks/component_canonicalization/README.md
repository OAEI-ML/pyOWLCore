# Component-canonicalization evidence

This directory pins the deterministic Functional Syntax inputs used by WP23's
public component-scaling gate. The byte counts and SHA-256 values in
`inputs.json` are generated locally; this lane performs no downloads and uses
no licensed corpus.

The `smoke` profile covers 1, 8, and 64 disconnected fixed-size components plus
one deliberately oversized connected component. The `release` profile adds the
50,000-component input also pinned as
`generated-component-scaling-functional` in `benchmarks/corpora.toml`.

Generate and then independently check a canonical JSON report with:

```console
python -m tools.benchmark.component_canonicalization generate \
  --profile smoke --output /tmp/component-smoke.json
python -m tools.benchmark.component_canonicalization check \
  /tmp/component-smoke.json
```

The report is correctness and resource-accounting evidence, not a timing or
portable performance claim. Real biomedical corpus execution and release
decisions are separate lanes.
