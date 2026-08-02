# 0.2.0 release checklist

The source tree targets package `0.2.0`, API `(0,2)`, model schema `2`, wire
`(1,2)`, adapter protocol `1`, and encoded structural schema `2`. This is a
version and implementation checkpoint, not evidence that the release has been
published or that an unexecuted gate passed.

The machine-readable authority is
[`reports/release/0.2.0/gates.json`](../reports/release/0.2.0/gates.json). Every
entry is fail-closed until evidence from the final source revision replaces its
blocked description.

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
- [ ] `consumer_matrix`: rerun the exact supported consumers after they widen
      their ranges and negotiate API `(0,2)` / encoded schema 2.
- [ ] `legal_review`: record current release-owner or counsel approval for the
      final native dependency and packaged notice inventory.
- [ ] `name_control`: record control of the production `pyowl-core` project for
      this release.
- [ ] `platform_artifact_audit`: build and audit the complete 25-native-wheel,
      one-pure-wheel, one-sdist candidate from one revision.
- [ ] `project_urls`: inspect the final artifacts and approve their repository,
      documentation, and issue URLs.
- [ ] `reference_performance`: attach the required pinned-corpus one-pass,
      memory, correctness, component-scaling, and Horned comparison evidence.
- [ ] `release_owner_approval`: approve the exact checksum-bound candidate and
      its documented limitations.
- [ ] `signatures`: verify provenance/signatures for the immutable candidate.
- [ ] `source_tag_verified`: create and verify `v0.2.0` at the exact tested
      source revision.
- [ ] `testpypi_rehearsal`: install and verify every identical candidate file
      from TestPyPI.
- [ ] `trusted_publishing`: verify the selected publishing identity and
      protected-environment configuration.

## Final corpus and native closeout

The public Functional `fixed-50000` component-scaling gate has passed with
checksum-bound additive-work evidence. A candidate NCIt observation completed
one native/default-limit document load, but exact final-artifact retention, its
model-schema-1 alpha/count reference, and same-machine RSS comparison remain
open; FMA remains unrun. The scoped installed-native/py-horned DOID comparison
also passed equality plus fresh/steady common-ready wall and fresh RSS gates.
It remains non-formal because the Python/direct-Horned lanes, approved required
corpus/reference machine, and evaluable steady RSS were outside that run.

Accordingly `reference_performance` remains blocked until the remaining
NCIt/FMA closeout and, for the complete normative claim, the missing comparator,
reference-host, and delivered-wheel/direct-engine overhead evidence are bound
to one final revision. Counts, fingerprints, alpha-equivalence where defined,
phase telemetry, peak RSS, raw paired samples, and exact input/tool hashes must
accompany each result. Licensed SNOMED lanes are reported as `not-run` when
unavailable and do not become synthetic passes.

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
