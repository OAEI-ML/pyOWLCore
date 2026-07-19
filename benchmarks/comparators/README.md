# Retained-native comparator contract

`comparators.toml` is the executable, fail-closed WP14 comparator manifest. It
records every lane, engine/artifact policy, allocator/thread/JVM setting,
reference-machine key, and phase boundary used by the redesign scaffold. It
deliberately distinguishes raw `horned-model-ready` from gating
`common-contract-ready`.

The current checked-in evidence is only a tiny Functional Syntax smoke of the
core Python common-contract adapter in fresh- and steady-process resident-byte
modes. It validates orchestration and output-fence mechanics; it is not a
representative comparator baseline or retained-native result.

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
common-contract Horned, py-horned, and OWLAPI. All five runner pins are
currently `pending` without runner artifact hashes, so every external lane is
non-runnable even if a launcher environment variable is configured.
Horned-OWL 1.4.0 still has one exact engine artifact pin shared by its raw and
common lanes; that engine pin does not complete either runner pin. The
installed retained-native-wheel lane is separate and rejects source-tree/native
builds; it requires an isolated delivered-wheel environment.

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

The shared Darwin entry remains explicitly `pending`; release evidence must use
an approved versioned machine. Resident-byte and file lanes are implemented:
file inputs are hash-checked and prepared before timing, use the same stable
source-bound document IRI as resident bytes, and include the implementation's
file open/read in the timer. Persistent external steady-process runners, paired
randomized block ordering, representative medium/large RDF/XML corpora,
executable ratio gates, native retention/copy counters, and phase profiles
remain open and must continue to be reported as unsupported or `not-run`.

`dependency-audit-shared-host.json` binds passing alias-aware source,
payload-manifest, and packaged-Python scans plus reproducible SHA-bound sdist and
pure-wheel checks to one clean Git commit. That limited audit explicitly records
platform linkage as `not-run` and is not native-wheel release evidence: a native
wheel, platform linkage, release SBOM/license review, and approved release
packaging remain open.
