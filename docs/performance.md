# Performance evidence and limits

Published numbers are evidence for the recorded workload only, not general
marketing claims. Unless a paragraph says otherwise, the measurements below
are historical `0.1.x` evidence and are not `0.2.0` release results.

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

These measurements predate the retained-native and anonymous-canonicalization
redesigns specified for WP14-WP23. They do not demonstrate a schema-2 retained
Rust ontology, Horned-OWL-equivalent loading, or an OWLAPI speedup. Those claims
remain blocked until the pinned common-readiness comparisons in the normative
performance specification pass.

OAEI NCIt/DOID and ORDO/OMIM members are hash-pinned, but strict full-pair core
mapping was not established in the recorded WP10 run. A later diagnostic on the
pinned NCIt–DOID pair did establish an exact composite and a 159,392,315-byte
wire image. Reusing fully validated eager-decoder rows reduced that image's full
decode from 272.795 seconds to 185.165 seconds (32.12%) and peak RSS from
1,003.79 MB to 996.49 MB. The downstream pyELK result remained exactly 2,227
unsatisfiable classes with the frozen Java ELK digest
`8dd56db2f864e757fb9fe04ca9b4cb6798e161597ff715f81175129db8bc27ab`.
This is a one-run local diagnostic, not a reference-machine regression baseline
or a claim about every Bio-ML pair. See
[`reports/performance/limitations.md`](../reports/performance/limitations.md)
and [`summary.json`](../reports/performance/summary.json) for exact options,
hashes, counters, and unresolved gates.

## 0.2.0 release evidence

The component-scoped v2 implementation records exact per-document component,
label, arc, root/span, open-interval, phase-work, refinement, permutation, and
anonymous-phase accounted-allocation counters.
`LoadReport.timings["native_anonymous_accounted_bytes"]` is the exact monotonic
`Session` budget delta from common anonymous-scoping entry through completed
canonical rows and occurrence digests. Ontology-key setup performed by the
format wrapper before that checkpoint is excluded. The value is conservative
accounted allocation work, not live allocation, allocator high-water usage, or
process RSS. These counters make the former document-global canonicalization
cost auditable, but instrumentation and focused tests are not a performance
pass.

No current `0.2.0` corpus result is accepted yet. The release gate remains
blocked until the final model-schema-2 native artifact records, on the selected
machine and exact source revision:

- one-pass default-limit loads and correctness anchors for the pinned NCIt and
  FMA documents, including same-machine peak RSS against their documented
  chunked incident baselines;
- the redistributable/generated Functional component-scaling gate, proving
  charged work scales with the sum of bounded component work;
- the required fresh/steady resident/file comparison against pinned direct
  Horned and py-horned contracts, with raw samples, confidence bounds, phase
  profiles, peak RSS, and output equality; and
- the checksum-bound delivered-wheel overhead and no-eager-materialization
  evidence required by the native redesign.

Licensed SNOMED RDF/XML and Functional Syntax lanes remain `not-run` when the
authorized inputs are unavailable and block only the private incident claim.
They are never replaced by generated data or counted as a public release pass.
The exact requirements and historical incident observations are in
[the component-canonicalization work package](../specs/workpackages/WP23-component-anonymous-canonicalization-v2.md).
