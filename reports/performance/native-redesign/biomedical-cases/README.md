# WP23 biomedical one-document cases

Each JSON file is one fresh-process, checksum-pinned observation produced by
`tools.benchmark.biomedical_gate`. Cases are run sequentially so another
ontology process cannot distort the recorded peak RSS. A case records exact
source, Python/package/native-extension, default-limit, backend, count,
fingerprint, anonymous-component, wall-time, CPU-time, and process-peak-RSS
evidence.

The retained generated Functional case cross-pins the native result to the
independent Python `fixed-50000` release evidence. Its
`native_anonymous_accounted_bytes` value is a monotonic Session-accounted
delta, not an allocator peak; `fresh_process_peak_rss_bytes` is the peak-memory
observation.

Validate that case with:

```console
python -m tools.benchmark.biomedical_gate check-case \
  --expected-native-sha256 27d07a79de9921b6d93f40d965fa6f8f6ef1bc1d0e8c07877face09d01b7dc27
```

FMA and NCIt are intentionally absent from this checkpoint. Their final rows
must use the same immutable native artifact and runtime, and must be run one at
a time. The final assembler records `same_machine_attested=false`, so incident
RSS comparisons remain non-evaluable. It also records the missing NCIt
model-schema-1 raised-limit alpha-equivalence baseline as `not-run`; FMA's
791,162-axiom and 104,942-declaration values remain reported regression
anchors, never parity oracles.
