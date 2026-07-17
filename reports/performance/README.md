# WP10 performance evidence

WP10's reproducible shared-host lanes pass every semantic, incremental-memory,
handoff, mmap, import-deduplication, parity, and bounded-adversarial assertion.
The result is an informative initial baseline, not an approved release-reference
baseline: CPU model, storage, power mode, and an isolated reference runner were
not available on this host.

## Results

All values below are medians on Darwin 25.5.0 x86_64, CPython 3.12.3, 12
logical CPUs, and 32 GiB physical RAM. Safety/resource defaults remained on;
inputs were resident and hash-verified; acquisition and post-timer output
validation were excluded. The raw reports retain every sample and the complete
environment.

| Scenario | Python | AUTO | Native | Outcome |
|---|---:|---:|---:|---|
| 9,999-axiom parse document | 3.209 s | 2.494 s | 2.876 s | native 1.12x throughput; no 2x parser claim |
| 9,999-axiom load/freeze | 9.710 s | 6.366 s | 5.883 s | native 1.65x throughput |
| 9,999-row axiom index | 0.726 s | 0.359 s | 0.361 s | native 2.01x speedup |
| 9,999-axiom mmap open | 0.363 s | 0.493 s | 0.399 s | 3.7–7.7% of matching full load |
| one-axiom overlay creation | 1.995 ms | 1.031 ms | 1.061 ms | 5,217 B peak traced allocation |
| two-base composite creation | 0.442 ms | 0.436 ms | 0.422 ms | 7,504 B peak traced allocation |
| in-process handoff | 0.279 ms | 0.199 ms | 0.210 ms | exact identity; all work counters zero |
| Uberon parse document | 2.143 s | — | — | 5 validated Python runs |
| Uberon load/freeze | 5.391 s | — | — | complete 12,103-triple mapping |
| Uberon mmap open | 0.331 s | — | — | 6.1% of full load |

On the large synthetic, mmap open traced 90,269 bytes versus 12,963,503 bytes
for full decode (0.70%). On Uberon it traced 52,339 bytes versus 9,064,961
bytes (0.58%). Both are below the 10% decoded-heap limit and the 25% startup
wall limit. Large overlay/composite peaks are far below their 16 MiB minimum
gate, and instrumentation proves the exact base arena identities are retained.

The shared-host handoff calibration is 1 ms p95. Observed p95 was 0.486 ms on
the large Python snapshot and 0.298 ms on Uberon, with 13,694 bytes peak
supplemental tracing and zero parser, resolver, wire, document-construction, or
snapshot-construction calls. The generated import diamond retained four
physical documents, made four parser calls, and parsed the shared digest once.

## Native decision and measured profiles

Native large parsing is beneficial but not 2x, so no 2x parse claim is made.
The artifact is justified by a different measured benefit: the safe-Rust
axiom partition removes the second Python canonical-key pass and is 2.01x
faster at 9,999 rows. AUTO keeps Functional sources below 256 KiB and indexes
below 4,096 rows on Python; the 20-run tiny baseline measured AUTO/Python ratios
of 0.865 for parse and 1.069 for load, within the 1.10 small-workload limit.

The retained profiles identify canonical encoding/digests, document freeze and
fingerprints, native-result materialization, and wire validation/materialization
as the dominant paths. No new product optimization was added in WP10. A global
canonical-byte cache would add retained state to every immutable model node;
the current evidence does not establish a net end-to-end memory benefit.
Safety checks were not bypassed. The existing measured native index operation
and conservative AUTO cutovers are retained.

## Reproduction

Preparation is explicit, outside every timed phase, and verifies exact hashes:

```console
PYTHONPATH=src:. python -m tools.benchmark.manifest --check
PYTHONPATH=src:. python -m tools.benchmark.manifest \
  --prepare uberon-common-anatomy-2026-06-23
```

The baseline and large lanes were produced with:

```console
PYTHONPATH=src:. python -m tools.benchmark.harness \
  --corpus generated-tiny-functional \
  --backend python --backend auto --backend native \
  --warmups 1 --repetitions 20 \
  --output benchmarks/baselines/shared-darwin25-x86_64-py312.json

PYTHONPATH=src:. python -m tools.benchmark.harness \
  --corpus generated-large-functional \
  --backend python --backend auto --backend native \
  --warmups 1 --repetitions 5 \
  --output reports/performance/raw/shared-darwin25-generated-large-py312.json
```

The regression workflow compares exact scenario sets, corpus manifests,
machine/runtime keys, statuses, and output fingerprints before applying the
10% median/RSS and 15% query/mmap p95 limits. The self-comparison demonstrates
the initial workflow; a real regression judgment begins with the next
equivalent candidate.

## Evidence inventory

- corpus manifest: `1059cde0173e9d9f787d16596158b6a134508740b9c27a87e786db11caf5b928`
- tiny baseline: `3edea8b0cadbc570dce4281986d9b74f3b46136fdf042e859ed1419519e5f8b8`
- large raw report: `0147eeb17f80fdf797a222aa1bf0020abfd6b922ea5e5f31e07cea2b077d8aea`
- Uberon raw report: `d613398a3b51a770150fb1ee1e761ba8a9579f813dafd00e73ab650136dae315`
- structured gate summary: `summary.json`
- corpus qualification observations: `biomedical-observations.json`
- measured profiles: `profiles/`
- regression workflow evidence: `regression-self.json` and `regression-self.md`

See `limitations.md` before treating any number as a release or marketing
claim. No Java runtime, build dependency, benchmark context, or artifact was
used.
