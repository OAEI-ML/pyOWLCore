# Reproducible performance corpora

`corpora.toml` is the fail-closed lock for benchmark inputs. Generated inputs
are reproduced locally; external inputs are downloaded only by an explicit
preparation command, outside every timed phase:

```console
python -m tools.benchmark.manifest --check
python -m tools.benchmark.manifest --prepare uberon-common-anatomy-2026-06-23
```

Preparation verifies the publisher artifact and selected archive-member hashes,
bounds download/decompression bytes, rejects unsafe archive paths, and publishes
atomically into `benchmarks/results/corpora/` (git-ignored). Normal tests,
package import, and benchmark execution perform no network access. Corpus bytes governed by
`manifest-only` are never committed or republished.

The four generated syntax rows contain the same declaration/subclass-chain
semantics in Functional-Style, OWL/XML, Turtle, and RDF/XML. The import diamond,
annotation/list-heavy graph, scaled synthetic, and deep adversarial rows cover
the remaining offline gates. Pinned Uberon, HPO, and OAEI Bio-ML records provide
real biomedical provenance and exact counts where strict mapping is runnable.

Machine baselines live in `benchmarks/baselines/`; raw samples and profiles are
kept in `reports/performance/`. A committed baseline is descriptive for its
recorded machine, not a portable absolute throughput promise.

## Running the harness

Run tools from the repository root with both the source tree and repository on
`PYTHONPATH` when the project is not installed editable:

```console
PYTHONPATH=src:. python -m tools.benchmark.harness \
  --corpus generated-tiny-functional \
  --backend python --backend auto --backend native \
  --warmups 1 --repetitions 20 \
  --output benchmarks/results/performance-run.json
```

Every phase validates its result after the timer stops. The report retains raw
wall/CPU/RSS samples, median/p90/p95/MAD/bootstrap intervals, one separate
`tracemalloc` run, corpus/output fingerprints, exact backend, and full machine,
Python, Rust, native-artifact, and Git metadata. Production safety and resource
defaults stay enabled. A missing native capability is an explicit optional
skip; Python remains fully runnable.

`resident-bytes-warm-process` means inputs are already hash-verified bytes and
the process is warmed as requested. `resident-bytes-fresh-process` describes a
new harness process, but does not claim that the operator dropped the OS page
cache. True cold trials and per-scenario peak RSS require external process/OS
orchestration on the versioned reference machine. The committed shared-host
smoke baseline is informative, not an approved reference-machine calibration.

Profile a phase separately from gate runs:

```console
PYTHONPATH=src:. python -m tools.benchmark.profile \
  --corpus generated-large-functional --backend python --phase parse \
  --iterations 1 --output reports/performance/profiles/python-parse.txt
```

Compare an equivalent candidate against a baseline:

```console
PYTHONPATH=src:. python -m tools.benchmark.regression \
  benchmarks/baselines/darwin-x86_64-py312.json candidate.json \
  --json reports/performance/regression.json \
  --markdown reports/performance/regression.md
```

The comparator refuses changed corpus manifests, scenario sets, output
fingerprints, statuses, or machine/runtime comparison keys. It enforces the
10% median/RSS and 15% query/mmap p95 release thresholds. Baseline changes
therefore require an explicit reviewed update instead of silently comparing
different work.
