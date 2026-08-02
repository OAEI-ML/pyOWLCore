# 0.2.0 release checklist

The source tree targets package `0.2.0`, API `(0,2)`, model schema `2`, wire
`(1,2)`, adapter protocol `1`, and encoded structural schema `2`. This is a
version and implementation checkpoint, not evidence that the release has been
published or that an unexecuted gate passed.

The machine-readable authority is
[`reports/release/0.2.0/gates.json`](../reports/release/0.2.0/gates.json).
The release-owner decision is recorded in
[`owner-release-authorization.md`](../reports/release/0.2.0/owner-release-authorization.md).
Only the exact-source advisory and platform entries remain fail-closed; the
Wheels aggregate must replace both before it can publish a release-ready
candidate.

## Implemented checkpoint

- [x] The package, API, model, wire, adapter, and encoded-schema values agree
      with `schemas/version-decision-v2.toml`.
- [x] Model-schema-1 and encoded-schema-1 data remain historical formats and
      are not reinterpreted as schema 2.
- [x] WP22's strict-by-default partial-RDF option and conflict routes are
      incorporated into API `(0,2)`.
- [x] Artifact inspection, import probing, dependency/SBOM generation,
      checksum binding, target-platform auditing, and the release-report tool
      default to package `0.2.0`.
- [x] Historical `0.1.0` and `0.1.1` evidence remains unchanged and explicitly
      labelled as historical.

These checks establish repository consistency only. They do not close the
release gates below.

## Required 0.2.0 gates

- [ ] `advisory_scan`: attach the final exact-source RustSec result.
- [x] `consumer_matrix`: the owner accepts the completed local 0.2 migrations
      for pyELK, pyHermiT, Projector, and OAEI; Exact-OM is outside this
      coordinated publication scope.
- [x] `legal_review`: the owner accepts the current dependency, notice, licence,
      and source-policy boundary and waives `LIC-001` as-is without claiming
      counsel review.
- [x] `name_control`: the owner confirms control of the production
      `pyowl-core` project for this release.
- [ ] `platform_artifact_audit`: build and audit the complete 25-native-wheel,
      one-pure-wheel, one-sdist candidate from one revision.
- [x] `project_urls`: the owner approves the configured OAEI-ML repository,
      documentation, and issue URLs.
- [x] `reference_performance`: the owner accepts the exact installed-native
      versus py-horned DOID common-contract result for 0.2.0 and waives the
      disclosed fuller reference-host closeout without calling it executed.
- [x] `release_owner_approval`: the owner authorizes the final validated source
      and only the exact candidate that passes the two remaining automated
      gates.
- [x] `signatures`: pre-upload signatures are waived for account-token
      publication; artifact hashes and post-upload verification remain
      mandatory.
- [x] `source_tag_verified`: the owner authorizes `v0.2.0` creation from the
      final validated source after its Wheels candidate passes.
- [x] `testpypi_rehearsal`: TestPyPI is waived in favor of direct publication
      followed by public-index verification.
- [x] `trusted_publishing`: direct account-scoped PyPI API-token publication is
      authorized; protected OIDC remains an optional stronger path.

## Final corpus and native closeout

The public Functional `fixed-50000` component-scaling gate has passed with
checksum-bound additive-work evidence. A candidate NCIt observation completed
one native/default-limit document load, but exact final-artifact retention, its
model-schema-1 alpha/count reference, and same-machine RSS comparison remain
open; FMA remains unrun. The scoped installed-native/py-horned DOID comparison
also passed equality plus fresh/steady common-ready wall and fresh RSS gates.
It remains non-formal because the Python/direct-Horned lanes, approved required
corpus/reference machine, and evaluable steady RSS were outside that run.

The release owner accepts the scoped DOID result as sufficient for `0.2.0`, so
`reference_performance` is closed by an explicit release decision rather than
by pretending the fuller matrix ran. NCIt/FMA closeout, the additional
comparators, approved reference host, delivered-wheel/direct-engine overhead,
and evaluable steady RSS remain requirements for any later complete normative
claim. Licensed SNOMED lanes remain `not-run` when unavailable and do not
become synthetic passes.

The final native extension must also rerun the WP19-WP22 structured-limit,
strict mapping, reification-evidence, and diagnostic-partial parity selections.
The hosted Wheels aggregate must replace the staged advisory and platform
entries only after all 27 artifacts pass; it cannot inherit a pass from a
different version or source revision.

## Historical release decisions

The `0.1.0` publication record and the corrective `0.1.1` owner override remain
under `reports/release/0.1.0/` and `reports/release/0.1.1/`. Their approvals,
consumer matrix, and performance observations describe the old API `(0,1)`,
model schema `1`, wire `(1,1)`, and encoded schema 1. They are preserved for
auditability and are not promoted into the `0.2.0` gate ledger.
