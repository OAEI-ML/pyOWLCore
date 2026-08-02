# WP23 biomedical one-document cases

Each JSON file is one fresh-process, checksum-pinned observation produced by
`tools.benchmark.biomedical_gate`. Cases are run sequentially so another
ontology process cannot distort the recorded peak RSS. A case records exact
source, Python/package/native-extension, default-limit, backend, count,
fingerprint, anonymous-component, wall-time, CPU-time, and process-peak-RSS
evidence.

The retained generated Functional case cross-pins the native result to the
independent Python `fixed-50000` release evidence in
`reports/performance/component-canonicalization-v2/release.json`. That release
profile passed with evidence SHA-256
`d950d8d2fba15b90e57084076967007876aca346d5a29f1deb78cb90af05611b`.
The native case file has SHA-256
`79e6bbbd155d9fd32d517029a2b0027ab432b81e00944e7e2440d234f8307083`,
uses extension SHA-256 `27d07a79de9921b6d93f40d965fa6f8f6ef1bc1d0e8c07877face09d01b7dc27`,
and reproduces document fingerprint
`d95eacbbad0f0fc91c567599ece59404ff8e372153763999153ddf6044460832`.
Its
`native_anonymous_accounted_bytes` value is a monotonic Session-accounted
delta, not an allocator peak; `fresh_process_peak_rss_bytes` is the peak-memory
observation. This is functional scaling evidence, not a portable performance
claim or a final-candidate artifact pin.

Validate that case with:

```console
python -m tools.benchmark.biomedical_gate check-case \
  --expected-native-sha256 27d07a79de9921b6d93f40d965fa6f8f6ef1bc1d0e8c07877face09d01b7dc27
```

FMA remains absent. NCIt has a successful temporary one-document capture at
`/private/tmp/pyowl-ncit-c65316c.json`, SHA-256
`c96be82e4db359f7fd1b54d6c63b699c9849f4d7610870df8f709fe43d4e58b4`,
using extension SHA-256
`226373ef1c8f83603ca1467515ea9c6502e982943777ca0b09cf8c1e506a8032`.
It must be retained or rerun and cross-assembled with the same immutable final
artifact. The missing NCIt model-schema-1 raised-limit alpha/count reference
and same-machine RSS attestation remain open. FMA's 791,162-axiom and
104,942-declaration values remain reported regression anchors, never parity
oracles.
