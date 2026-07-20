# Native-redesign comparator baseline

Status: executable manifest, core-Python smoke, and pinned py-horned lifecycle
smoke implemented; approved-machine comparative evidence remains open.

`shared-host-py-horned-smoke.json` is the current development-only external
runner checkpoint. It is bound to clean commit `3dc4642`, the exact py-horned
1.4.0 sdist SHA-256, and the exact runner SHA-256. Three paired repetitions in
each fresh/steady and resident/file combination produce 12 passing
common-contract assertions. Its persistent child completes eight requests with
eight distinct ontology identities, empty stderr, and clean shutdown. The
reference machine is unapproved, the selected corpus is tiny, and no installed-
wheel numerator is present, so the configured ratio gates correctly remain
failed.

The committed `shared-host-smoke.json` exercises one pinned 1,450-byte generated
Functional Syntax input through the core Python common-contract lane, once in a
steady process and once in a fresh process. It records every other requested
lane as `not-run`; no such row contributes a pass or timing ratio. Each of the
five external lanes has a separate runner pin, and every one was `pending`
without a runner artifact hash when this historical artifact was captured.
Its rows therefore report `artifact or external runner pin is pending`. The
current manifest now has a complete SHA-bound py-horned runner; that later
implementation does not retroactively alter this report. The
installed-native-wheel row separately reports that a source-tree/native build
is ineligible and an isolated delivered wheel is required.

The smoke proves the following repository-owned mechanics:

- the comparator ledger is linked to the exact corpus manifest SHA-256;
- raw Horned and common-contract Horned are separate lanes;
- all 17 phase fences are complete and equality is outside every timer;
- the Python adapter reconstructs and cross-checks the document, structural,
  logical, and signature fingerprint preimages;
- the Python result includes canonical structural inventories, identity,
  import/provenance, diagnostic inventories, raw samples, wall/CPU/RSS, and
  provisional Python allocation/object metrics;
- fresh-process startup-to-ready and call-to-ready are distinct metrics; and
- post-timer equality compares only already-published counts/digests.

Retained-handle/facade publication is `n/a` for this pure-Python lane. The
ordinary Python snapshot and common-contract output are still completed inside
the lane timer; this smoke does not demonstrate a retained-native publication
boundary or bulk encoded-view traversal.

This is not comparative performance evidence. The reference-machine entry is
still `pending`; only the tiny generated corpus was run; all external runner
pins were pending and non-runnable at capture time; and the installed retained-
native wheel/bulk path was not run. File lanes, persistent external steady-process execution,
paired implementation-order randomization, and executable ratio gates were not
implemented when this historical artifact was captured and remain absent from
it. Therefore no Horned/OWLAPI ratio, threshold pass, bulk-consumer result, or
retained-native claim is made.

Historical pre-redesign profile evidence remains in
`reports/performance/profiles/`. Those generated-large profiles locate parser,
canonical encoding, native-result materialization, index, and wire work. The
pinned representative biomedical inputs were not run under the successor
contract on this host. Native retention/copy counters, complete successor phase
profiles, and approved-machine NCIT/DOID/OAEI evidence therefore remain
`not-run`; the tiny Python metrics cannot substitute for them.

## Reproduce

```console
PYTHONPATH=src:. python -m tools.benchmark.comparators.runner --check
PYTHONPATH=src:. pytest -q tests/benchmark/comparators
PYTHONPATH=src:. python -m tools.benchmark.comparators.runner \
  --corpus generated-tiny-functional \
  --lane pyowl-python-common \
  --lane pyowl-native-wheel-common \
  --lane pyowl-direct-rust-common \
  --lane horned-owl-raw \
  --lane horned-owl-common \
  --lane py-horned-common \
  --lane owlapi-common \
  --process-mode steady-process \
  --process-mode fresh-process \
  --warmups 0 --repetitions 1 \
  --allow-partial \
  --output reports/performance/redesign-baseline/shared-host-smoke.json
```

The smoke command requires `--allow-partial`: its report is contract-valid,
contains no runner errors, but is comparatively incomplete. Partial mode
tolerates only explicit unavailable/ineligible rows; any selected-lane runner
or protocol error still produces a nonzero exit. Omitting the flag deliberately
produces a nonzero exit until the complete required matrix passes.

`dependency-audit-shared-host.json` is separate release evidence with a limited
passing scope. It records passing source metadata/import/payload-manifest scans
and bounded packaged-Python scans plus SHA-bound inspection of the sdist and
pure wheel, with no excluded comparator dependency found. Its canonical source
identity and Git provenance bind the inspection to clean commit
`f89c9d005698ec969f7a073b0ccea49c801a63f2`. It is not a platform linkage audit
and includes no native wheel; native-wheel linkage, release SBOM/license review,
and approved release packaging remain open.

The two pure artifacts are reproducible detached inputs, not package payloads.
Two independent exports/builds with `SOURCE_DATE_EPOCH=1784450414` produced the
same wheel SHA-256
`44c596c6281a9835a70986474e51d197485a1740ccaba5cef817644d1a946922`
and sdist SHA-256
`fbfda6ab666952fedbf3a8d007189f77381be70c93d1938fafc7ac285e822d6c`:

```console
git archive --format=tar --output=source.tar f89c9d005698ec969f7a073b0ccea49c801a63f2
mkdir source
tar -xf source.tar -C source
cd source
SOURCE_DATE_EPOCH=1784450414 python -m build --no-isolation --sdist --wheel --outdir dist
PYTHONPATH=src:. python -m tools.benchmark.comparators.dependency_audit \
  --root . \
  --artifact dist/pyowl_core-0.1.0.dev0-py3-none-any.whl \
  --artifact-kind wheel \
  --artifact-sha256 44c596c6281a9835a70986474e51d197485a1740ccaba5cef817644d1a946922 \
  --artifact dist/pyowl_core-0.1.0.dev0.tar.gz \
  --artifact-kind sdist \
  --artifact-sha256 fbfda6ab666952fedbf3a8d007189f77381be70c93d1938fafc7ac285e822d6c
```

Release evidence must run medium/large RDF/XML and annotation/list-heavy
corpora with paired randomized blocks, at least the repetitions in
`specs/performance.md`, an approved machine, exact engine/adapter/image hashes,
separate resident/file lanes, and the full fresh/steady matrix. It must also
capture executions of the implemented persistent external-runner lifecycle,
phase profiles, object/copy/RSS counters, the remaining
native-wheel/SBOM/release-packaging audits, and results from the implemented
paired ratio gates. None of that evidence is produced by this historical smoke
command, and the smoke must not be promoted into an equivalence claim.
