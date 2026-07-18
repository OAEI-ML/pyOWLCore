# Performance evidence and limits

Published numbers are evidence for the recorded workload only, not general
marketing claims.

The current shared-host run used Darwin 25.5.0 x86_64, CPython 3.12.3, Rust
1.97.1, 12 logical CPUs, and 32 GiB RAM. CPU model, storage, and power mode were
not available, so the host is not an approved release reference machine.
Safety/resource defaults were enabled; acquisition and output validation were
outside timed phases.

Pinned evidence includes:

| Corpus | Revision/hash basis | Repetitions | Status |
|---|---|---:|---|
| generated tiny Functional | generator-v1/classes-8 | 20 | shared-host gate |
| generated 9,999-axiom Functional | generator-v1/classes-5000 | 5 | shared-host gate |
| Uberon common anatomy | v2026-06-23, pinned SHA-256 | 5 | informative biomedical |
| HPO base | v2026-06-23, pinned SHA-256 | one qualification | not a regression gate |

The 9,999-axiom run measured native/Python throughput ratios of 1.12 for parse
and 1.65 for load/freeze. Native axiom-index construction measured 2.01x. No 2x
parser claim is made. Large mmap open used under 1% of full-decode traced
allocation; in-process handoff retained exact identity with zero parser,
resolver, wire, or construction calls.

OAEI NCIt/DOID and ORDO/OMIM members are hash-pinned, but strict full-pair core
mapping was not established in the recorded WP10 run. Those observations are
not presented as equivalent passing biomedical evidence. See
[`reports/performance/limitations.md`](../reports/performance/limitations.md)
and [`summary.json`](../reports/performance/summary.json) for exact options,
hashes, counters, and unresolved gates.

