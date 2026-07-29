# Retained-native comparator contract

`comparators.toml` is the executable, fail-closed WP14 comparator manifest. It
records every lane, engine/artifact policy, allocator/thread/JVM setting,
reference-machine key, and phase boundary used by the redesign scaffold. It
deliberately distinguishes raw `horned-model-ready` from gating
`common-contract-ready`.

The checked-in evidence has six explicitly development-only artifacts.
`shared-host-smoke.json` is the historical tiny Functional Syntax smoke of the
core Python adapter; it predates the paired scheduler and executable ratio
gates, so its older fields remain accurate. `shared-host-py-horned-smoke.json`
binds the first complete external runner to clean commit `3315c22`. Across
fresh/steady and resident/file modes, three paired repetitions per scenario
produce 12 passing common-contract assertions. Its persistent process attests
eight requests, eight distinct ontology instances, bounded empty stderr, and a
clean shutdown. `shared-host-horned-raw-smoke.json` binds raw runner v1 and its
exact executable to clean commit `f6845ec`; the same four mode combinations and
three repetitions publish one stable raw inventory, while one persistent child
serves eight requests with eight distinct ontology identities and clean
shutdown. `shared-host-horned-common-smoke.json` binds the shared raw v2/common
v1 executable to clean commit `f12a4f1`. It records three paired repetitions in
all four mode combinations, 12 passing Python/common and resident/file equality
assertions, and clean eight-request persistent lifecycles for both Horned
boundaries. `shared-host-direct-smoke.json` binds direct runner v1 to clean
commit `588853e`; Functional Syntax and RDF/XML each pass resident/file and
fresh/steady exact contracts across 24 paired assertions, while one persistent
child serves 16 requests with 16 distinct ontology identities and clean
shutdown. `shared-host-owlapi-smoke.json` binds isolated OWLAPI runner v1 to
clean commit `c7e8b72`. Six generated corpora cover all four supported
syntaxes, annotation/list mapping, and unresolved imports in both input and
process modes; three paired repetitions produce 72 passing equality assertions,
while one persistent child serves 48 requests with distinct ontology identities,
empty stderr, and clean shutdown. The reference machine is still unapproved and
the complete paired matrix was not selected, so these are lifecycle and
correctness evidence—not performance gates.

Validate the ledger without installing or invoking any comparator:

```console
PYTHONPATH=src:. python -m tools.benchmark.comparators.runner --check
```

Run the current Python reference on pinned resident bytes:

```console
PYTHONPATH=src:. python -m tools.benchmark.comparators.runner \
  --corpus generated-tiny-functional \
  --lane pyowl-python-common \
  --process-mode steady-process \
  --seed 0 \
  --repetitions 5 \
  --allow-partial \
  --output reports/performance/redesign-baseline/shared-host-python.json
```

`--allow-partial` is required for an intentionally incomplete smoke run. Without
that explicit opt-in, a contract-valid report still exits nonzero unless the
complete comparative matrix passes. The opt-in never masks a runner or protocol
error; it tolerates only explicit unavailable/ineligible rows in otherwise
contract-valid development evidence.

External runner commands are supplied explicitly through the environment names
in the ledger. Normal tests never download, install, import, or invoke Horned,
py-horned, OWLAPI, a JVM, or a comparator runner. Missing launchers and pending
artifact pins produce `not-run`; they can never become a pass.

Every external lane has its own runner pin: direct retained Rust, raw Horned,
common-contract Horned, py-horned, and OWLAPI. All five runners are complete
and SHA-256 bound. Horned-OWL 1.4.0 has one
exact engine and executable artifact shared by its raw and common lanes, while
the boundary and runner revision remain lane-bound. The installed retained-
native-wheel lane is separate and rejects source-tree/native builds; it
requires an isolated delivered-wheel environment.

The direct retained-engine runner is an excluded Rust binary linked to the
native crate as an `rlib`; it neither imports Python nor crosses PyO3 objects.
Reproduce and authenticate the pinned Darwin x86_64 executable with:

```console
PYTHONPATH=. python -m tools.benchmark.comparators.build_direct_runner --print-sha256
printf '%s  %s\n' \
  0d901712131cd64c1e51970383f0c79591bc1d6fa28348f9edd303ddd2ad23fb \
  benchmarks/comparators/runners/direct/target/release/pyowl-core-direct-comparator \
  | shasum -a 256 -c -
export PYOWL_CORE_DIRECT_RUNNER="$PWD/benchmarks/comparators/runners/direct/target/release/pyowl-core-direct-comparator"
PYTHONPATH=src:. python -m tools.benchmark.comparators.linkage_audit \
  --binary benchmarks/comparators/runners/direct/target/release/pyowl-core-direct-comparator \
  --expected-runner-sha256 \
  0d901712131cd64c1e51970383f0c79591bc1d6fa28348f9edd303ddd2ad23fb \
  --output \
  reports/performance/redesign-baseline/dependency-audit-direct-linkage-darwin-x86_64.json
```

The build helper rejects inherited compiler seams, remaps the checkout, target,
and Cargo source paths, and replaces Cargo's checkout-dependent metadata for
the two local Rust crates without changing Cargo's artifact names. On Darwin
it also suppresses `LC_UUID`; the resulting pinned executable is byte-identical
across distinct checkout and target paths. Windows linkage CI keeps the path
remaps but deliberately omits the POSIX Python wrapper, so it publishes linkage
evidence rather than a cross-checkout reproducibility claim.

Runner v7 verifies its embedded native `Cargo.lock`, executable hash, exact
semantic options, source/document identity, allocator, thread ceiling, lane,
and boundary. Functional Syntax uses the retained parser arena and RDF/XML uses
the streaming mapper's retained arena. Both construct and fully validate the
common contract inside the timer for resident/file and fresh/persistent modes.
OWL/XML and Turtle are explicit `ineligible` results because no native retained
parser is advertised for those syntaxes.

The linkage audit independently checks both dynamic dependencies and imported
symbols. It fails closed when the platform inspector is absent, its output is
unrecognized, the executable changes during inspection, or any Python runtime
dependency or `Py*` import remains. Native-safety CI repeats the audit on
Darwin, Linux, and Windows builds; release evidence requires a passing artifact
from every claimed target platform.

The Horned runner is an excluded development binary, built from its own exact
Cargo lock and Rust 1.97.1 toolchain. Reproduce the recorded Darwin x86_64
runner and authenticate it before selecting either lane:

```console
cd benchmarks/comparators/runners/horned
cargo +1.97.1 build --locked --release
printf '%s  %s\n' \
  622c0655f8c66d8fca4024c2a050d13a56b9236f591959942c34e786baad840c \
  target/release/pyowl-core-horned-comparator | shasum -a 256 -c -
export PYOWL_CORE_HORNED_RUNNER="$PWD/target/release/pyowl-core-horned-comparator"
```

Raw runner v5 and common runner v6 verify the embedded Horned 1.4.0 crates.io
checksum, their shared executable SHA-256, exact semantic options,
source/document identity, allocator, thread ceiling, lane, and boundary before
parsing. The raw boundary builds Horned's set, IRI, component-kind, and
declaration indexes within the timer, then traverses the owned model to publish
bounded axiom, annotation, import, typed-signature, diagnostic, and
logical-object counts. Its inventory digest uses the same canonical scalar
preimage that the parent independently recomputes.

The independent common boundary maps the Horned model to every supported
canonical structural node, freezes bounded anonymous-node labels, constructs
the identity/import/diagnostic/provenance inventories and all four fingerprint
preimages, and validates the complete common-contract ledger inside its timed
adapter phase. Inputs whose semantics Horned 1.4.0 cannot preserve, including
nested Functional or OWL/XML annotations, fail closed as `ineligible` instead
of publishing a reduced contract. Functional Syntax SWRL surface tokens are
adapted to Horned's equivalent parser spelling without rewriting comments,
IRIs, or strings.

Functional Syntax, OWL/XML, and RDF/XML are supported; Turtle is explicitly
`ineligible` because the pinned Horned API exposes no Turtle reader selection.
Resident and prepared-file input, fresh and authenticated persistent process,
strict framing, monotonic sequences, fresh ontology identities, and clean
shutdown all use the same audited parent contract as the py-horned runner.

The py-horned runner is deliberately absent from package artifacts and ordinary
test dependencies. Reproduce its isolated development environment from the
exact pinned sdist as follows, substituting paths outside the repository:

```console
python -m pip download --no-deps --no-binary=:all: \
  --dest /tmp/py-horned-artifact py-horned-owl==1.4.0
printf '%s  %s\n' \
  7146d0887c5ec119e423e56c9221cc0ca7da54739be36ce3ed916503348f942d \
  /tmp/py-horned-artifact/py_horned_owl-1.4.0.tar.gz | shasum -a 256 -c -
python -m venv /tmp/py-horned-venv
/tmp/py-horned-venv/bin/python -m pip install \
  /tmp/py-horned-artifact/py_horned_owl-1.4.0.tar.gz
export PATH="/tmp/py-horned-venv/bin:$PATH"
export PYOWL_CORE_PY_HORNED_RUNNER="$PWD/benchmarks/comparators/runners/py_horned_common.py"
```

The runner parses with py-horned-owl, maps its independent object graph to the
public pyowl-core structural contract, and includes the complete mapping,
freeze, fingerprint, inventory, and validation cost inside the timer. Version
1.4.0 exposes Functional Syntax, OWL/XML, and RDF/XML readers but no Turtle
reader selection. Turtle requests therefore return explicit `ineligible`
evidence; they are never sent to the RDF/XML reader or counted as passes.
For RDF/XML, runner v9 performs a bounded, timed preparse only when axiom
reification is present. Equivalent anonymous or blank-node `owl:Axiom`
occurrences are physically coalesced before py-horned sees them, so qualifier
triples are unioned deterministically instead of depending on Horned's
last-write traversal. Unsafe XML, ambiguous metadata, named axiom resources,
and shapes that cannot be preserved exactly fail closed.
Before either a fresh request or persistent handshake, runner v9 also requires
distribution version 1.4.0, exact `direct_url.json` provenance for the pinned
sdist SHA-256, and a byte-for-byte match for every SHA-256 entry in the
installed distribution's RECORD. A renamed, editable, differently sourced, or
post-install modified engine fails before producing samples.

The OWLAPI v5 runner is likewise excluded from package artifacts and dependencies.
It pins OWLAPI distribution 5.5.1, an exact 521-file Temurin 21.0.7+6 runtime,
the deterministic runner JAR, launcher, 8 GiB fixed heap, G1GC with
`AlwaysPreTouch`, and one active processor. Its complete reproduction and
runtime-authentication procedure is in
`runners/owlapi/README.md`. The independent Java mapper constructs and validates
the complete model-schema/common-contract ledger inside the timer. Functional
Syntax, OWL/XML, RDF/XML, and Turtle use explicit readers; anonymous-individual
identity and any RDF occurrence ordering that cannot be recovered from OWLAPI
semantics return `ineligible` instead of a reduced result.

Every fresh external command uses
`pyowl-core/comparator-fresh-runner/v1`. Canonical length-prefixed
`pyowl-core/comparator-fresh-request/v1` wraps one unchanged
`pyowl-core/comparator-adapter-request/v2`; the final
`pyowl-core/comparator-fresh-response/v1` likewise wraps one unchanged
`pyowl-core/comparator-adapter-result/v1`. Request v2 adds the
source-digest-derived document IRI required to make resident-byte and
prepared-file semantic identities exact.
Common-ready results must include a validated
`pyowl-core/comparator-common-contract/v1` object. The runner performs only
already-published digest/count equality after timing. Raw Horned results instead
publish a bounded integer `raw_inventory` whose SHA-256 is recomputed from the
canonical v1 scalar preimage, and are never eligible as an equivalence
denominator.

For a fresh-process lane the child first builds and fully validates the adapter
result, refreshes peak RSS after the complete result/artifact shape exists, and
sends only `pyowl-core/comparator-fresh-completed/v1`. The acknowledgement has
exact fields for protocol, unsigned sequence zero, actual child PID, and the
lowercase SHA-256 of ASCII `"{pid}:0:0"`. The parent authenticates all of them
and immediately captures startup-to-ready wall time and supervisor CPU before
constructing or sending `pyowl-core/comparator-fresh-publish/v1`. It then writes
that exact release frame and closes child stdin. The child requires immediate
EOF after the publish frame; any trailing byte fails without a response. Only
after the valid publish and EOF may it construct, serialize, and flush the
response. The parent requires the same ontology identifier, no early or trailing
output, a bounded stderr stream, and a clean zero exit.

Successful fresh results also report `metrics.startup_to_ready_cpu_ns`, the
absolute child process CPU sampled at that same fully assembled pre-completed
boundary. It must be at least the call/load delta in `metrics.cpu_ns` and is
absent from steady and non-success results. `transport_metrics.parent_cpu_ns`
is deliberately different: it measures only supervisor/harness CPU through
authenticated completion. Response serialization and transport occur after all
fresh wall, child CPU, and RSS endpoints.

A steady-process lane instead starts one exact SHA-256-verified executable
before warm-ups and samples, and keeps that single process for the lane lifecycle.
The child must first send an exact
`pyowl-core/comparator-persistent-handshake/v3` attestation for protocol
`pyowl-core/comparator-persistent-runner/v3`: lane and boundary identity, its
actual child PID, request/prepared/execute/completed/publish/result schemas,
fresh-ontology-per-request support, and the complete artifact and runner pins
must all match before any request is eligible to run. Startup and handshake time
are lifecycle evidence and are outside call-to-ready samples.

Persistent handshake, request, acknowledgement, publication, response, and
shutdown messages use a canonical decimal byte length, newline, JSON payload,
and terminal newline. Protocol v3 is an authenticated, publication-gated
request. The parent first sends
`pyowl-core/comparator-persistent-request/v3`; the child validates its strict
envelope and decodes and verifies the adapter request before replying with
`pyowl-core/comparator-persistent-prepared/v1`. That acknowledgement repeats the
protocol, exact monotonic sequence, and authenticated child PID and establishes
the quiescent RSS boundary. The parent captures current RSS, starts interval
sampling, and sends a strict `pyowl-core/comparator-persistent-execute/v1` frame
containing that same sequence and PID. Timed ontology work cannot begin before
this execute frame.

After building and fully validating the result, the child derives a fresh
lowercase SHA-256 ontology-instance identifier and sends only a strict
`pyowl-core/comparator-persistent-completed/v1` frame containing the protocol,
sequence, PID, and identifier. It then blocks without serializing the response.
The parent authenticates that completion, captures the call-to-ready wall and
supervisor CPU endpoints, stops and joins the RSS sampler, and only then sends the matching
`pyowl-core/comparator-persistent-publish/v1` frame. The child rejects any field,
type, sequence, PID, or identifier mismatch before serializing
`pyowl-core/comparator-persistent-response/v3`; the parent also requires that
response to repeat the completed identifier. This two-way barrier makes response
serialization provably post-measurement; a one-way completion signal would race
the sampler's terminal observation. Call-to-ready wall time starts before the
request frame and includes request preparation but ends at authenticated
completion, before sampler teardown, publication, and response transport.

Every steady sample nests
`pyowl-core/comparator-rss-interval/v1` under
`transport_metrics.rss_interval`. Its exact fields are `source`, `pid`,
`quiescent_current_bytes`, `interval_peak_bytes`,
`incremental_peak_bytes`, `sample_count`, and `maximum_sample_gap_ns`, plus the
schema. The increment must equal interval peak minus quiescent current RSS and
the interval must contain at least two samples. External runners use a
pre-spawned sampler helper prepared before the parent call clock, then baselined
and armed against the authenticated child PID after the prepared
acknowledgement. It runs
through authenticated completion, before publication and response serialization.
In-process Python and delivered-wheel core lanes likewise use a spawned helper
process to sample the core process independently of its GIL from the corresponding
quiescent boundary through full contract validation. The helper is stopped before
legacy RSS, garbage-collector object, allocated-block, and result-wrapper
instrumentation. Both paths request a 1 ms interval. A
maximum observed gap above 10 ms is rejected as insufficient-quality steady RSS
gate evidence;
lifetime `ru_maxrss` is not substituted for the interval measurement.

Nonblocking selector I/O enforces request, prepared and completed
acknowledgements, publish, response, cumulative stderr, handshake, and
per-response time limits. Partial, malformed, oversized, replayed, unsolicited,
extra, late, or out-of-order frames fail the entire lane closed. Fresh runners
also require publish-side EOF and a clean zero exit. Shutdown uses
`pyowl-core/comparator-persistent-shutdown/v3`, requires the matching v3
acknowledgement and clean exit, and retains process-group
termination plus a kill fallback on error or timeout. A forked client cannot
use or signal the parent-owned runner. The lifecycle audit records the
authenticated handshake, PID, startup, request/response and unique-instance
counts, bounded stderr, and shutdown result.

The runner accepts an exact unsigned 64-bit `--seed` and records it with the
schedule and every measured raw sample. Each measured repetition is one paired
block; implementation order is shuffled independently in every scenario/block
by a stable SHA-256 rank derived from that seed. Steady-process warm-ups use the
same paired scheduling, every selected implementation receives the same warm-up
count, and an equal out-of-timer cleanup barrier follows every invocation.

The ratio evaluator is executable and fixed to the normative minimum gates. It
uses only resident-byte fresh- and steady-process pairs, computes per-corpus
median native/comparator ratios, and aggregates the required non-synthetic
medium/large set by geometric mean under stratified paired bootstrap resampling.
Bootstrap indexes come from the reported v1 SHA-256 counter stream with
rejection sampling, not a runtime-dependent standard-library PRNG. Fresh-process
wall gates use startup-to-ready measurements: `metrics.startup_to_ready_ns` for
the isolated native-wheel worker and `transport_metrics.parent_wall_ns` for
external runners. Steady-process wall gates use call-to-ready
`metrics.wall_ns` for in-process core lanes and authenticated
`transport_metrics.parent_wall_ns` for external persistent lanes. The separately
specified installed-wheel/direct overhead remains call-to-ready in both modes.
The upper endpoint of the 95% interval must be `<= 1.10` for wall time and
`<= 1.15` for incremental peak RSS; every required large-corpus median must be
`<= 1.25`. Direct Rust is paired only with Horned common readiness, the installed
wheel only with py-horned common readiness, and installed-wheel median
call-to-ready overhead over direct Rust must be `<= 1.15`. Raw
`horned-model-ready` is hard-excluded from equivalence denominators. Missing,
invalid, nonpositive, unpaired, contract-mismatched, or unavailable evidence
leaves the gate configured but failed with scenario-specific reasons.

The shared Darwin entry remains explicitly `pending`; release evidence must use
an approved versioned machine. An approved run supplies the exact operator
observations with `--reference-storage` and `--reference-power-mode`; when the
platform CPU probe is unavailable it also supplies `--reference-cpu-model`.
These bounded, control-free values and their provenance are retained in the
environment evidence. Storage and power must be operator-supplied, the CPU
value must agree with a successful platform probe, and every observed field
must exactly match the approved manifest row, so omitted or conflicting
observations fail closed.

Resident-byte and file lanes and the audited
persistent lifecycle are implemented and contract-tested. File inputs are
hash-checked and prepared before timing, use the same stable source-bound
document IRI as resident bytes, and include the implementation's file open/read
in the timer. The complete direct retained-Rust, raw/common Horned, py-horned,
and OWLAPI runners can exercise that lifecycle.
Representative medium/large approved-machine samples, native retention/copy
counters, and phase profiles remain open. The configured ratio gates therefore
fail closed; no performance threshold has passed merely because its evaluator,
lifecycle, and all five external runner lanes now exist.

`dependency-audit-shared-host.json` binds passing alias-aware source,
payload-manifest, and packaged-Python scans plus reproducible SHA-bound sdist and
pure-wheel checks to one clean Git commit. That limited audit explicitly records
platform linkage as `not-run` and is not native-wheel release evidence: a native
wheel, platform linkage, release SBOM/license review, and approved release
packaging remain open.
