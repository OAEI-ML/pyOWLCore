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
cd benchmarks/comparators/runners/direct
cargo +1.97.1 build --locked --release
printf '%s  %s\n' \
  a890618c136ef0712c8f09d7963241dacb51a811b96a803da18c7b2774515be0 \
  target/release/pyowl-core-direct-comparator | shasum -a 256 -c -
export PYOWL_CORE_DIRECT_RUNNER="$PWD/target/release/pyowl-core-direct-comparator"
```

Runner v2 verifies its embedded native `Cargo.lock`, executable hash, exact
semantic options, source/document identity, allocator, thread ceiling, lane,
and boundary. Functional Syntax uses the retained parser arena and RDF/XML uses
the streaming mapper's retained arena. Both construct and fully validate the
common contract inside the timer for resident/file and fresh/persistent modes.
OWL/XML and Turtle are explicit `ineligible` results because no native retained
parser is advertised for those syntaxes.

The Horned runner is an excluded development binary, built from its own exact
Cargo lock and Rust 1.97.1 toolchain. Reproduce the recorded Darwin x86_64
runner and authenticate it before selecting either lane:

```console
cd benchmarks/comparators/runners/horned
cargo +1.97.1 build --locked --release
printf '%s  %s\n' \
  ffd20194b7c3715d6d07ec8ba9167d590ed484c278305754148711c44ae8887b \
  target/release/pyowl-core-horned-comparator | shasum -a 256 -c -
export PYOWL_CORE_HORNED_RUNNER="$PWD/target/release/pyowl-core-horned-comparator"
```

Raw runner v2 and common runner v1 verify the embedded Horned 1.4.0 crates.io
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
Before either a fresh request or persistent handshake, runner v2 also requires
distribution version 1.4.0, exact `direct_url.json` provenance for the pinned
sdist SHA-256, and a byte-for-byte match for every SHA-256 entry in the
installed distribution's RECORD. A renamed, editable, differently sourced, or
post-install modified engine fails before producing samples.

The OWLAPI runner is likewise excluded from package artifacts and dependencies.
It pins OWLAPI distribution 5.5.1, an exact 521-file Temurin 21.0.7+6 runtime,
the deterministic runner JAR, launcher, 8 GiB fixed heap, G1GC with
`AlwaysPreTouch`, and one active processor. Its complete reproduction and
runtime-authentication procedure is in
`runners/owlapi/README.md`. The independent Java mapper constructs and validates
the complete model-schema/common-contract ledger inside the timer. Functional
Syntax, OWL/XML, RDF/XML, and Turtle use explicit readers; anonymous-individual
identity and any RDF occurrence ordering that cannot be recovered from OWLAPI
semantics return `ineligible` instead of a reduced result.

Each external command reads one
`pyowl-core/comparator-adapter-request/v2` JSON object on standard input and
writes one `pyowl-core/comparator-adapter-result/v1` object to standard output.
Request v2 adds the source-digest-derived document IRI required to make
resident-byte and prepared-file semantic identities exact.
Common-ready results must include a validated
`pyowl-core/comparator-common-contract/v1` object. The runner performs only
already-published digest/count equality after timing. Raw Horned results instead
publish a bounded integer `raw_inventory` whose SHA-256 is recomputed from the
canonical v1 scalar preimage, and are never eligible as an equivalence
denominator.

Fresh-process lanes use that one-request/one-result command directly. A
steady-process lane instead starts one exact SHA-256-verified executable before
warm-ups and samples, and keeps that single process for the lane lifecycle.
The child must first send an exact
`pyowl-core/comparator-persistent-handshake/v1` attestation for protocol
`pyowl-core/comparator-persistent-runner/v1`: lane and boundary identity, its
actual child PID, request/result schemas, fresh-ontology-per-request support,
and the complete artifact and runner pins must all match before any request is
eligible to run. Startup and handshake time are lifecycle evidence and are
outside call-to-ready samples.

Persistent handshake, request, response, and shutdown messages use a canonical
decimal byte length, newline, JSON payload, and terminal newline. Requests and
responses carry an exact monotonic sequence; every successful response also
carries a new lowercase SHA-256 ontology-instance identifier. Nonblocking
selector I/O enforces request, response, cumulative stderr, handshake, and
per-response time limits. Partial, malformed, oversized, replayed, unsolicited,
extra, or late frames fail the entire lane closed. Shutdown requires a
versioned acknowledgement and clean exit, with process-group termination and a
kill fallback on error or timeout. A forked client cannot use or signal the
parent-owned runner. The lifecycle audit records the authenticated handshake,
PID, startup, request/response and unique-instance counts, bounded stderr, and
shutdown result.

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
external runners. Steady-process wall gates use call-to-ready `metrics.wall_ns`;
the separately specified installed-wheel/direct overhead remains call-to-ready
in both modes.
The upper endpoint of the 95% interval must be `<= 1.10` for wall time and
`<= 1.15` for incremental peak RSS; every required large-corpus median must be
`<= 1.25`. Direct Rust is paired only with Horned common readiness, the installed
wheel only with py-horned common readiness, and installed-wheel median
call-to-ready overhead over direct Rust must be `<= 1.15`. Raw
`horned-model-ready` is hard-excluded from equivalence denominators. Missing,
invalid, nonpositive, unpaired, contract-mismatched, or unavailable evidence
leaves the gate configured but failed with scenario-specific reasons.

The shared Darwin entry remains explicitly `pending`; release evidence must use
an approved versioned machine. Resident-byte and file lanes and the audited
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
