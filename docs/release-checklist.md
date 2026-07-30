# 1.0 readiness and release checklist

This ledger records the 2026-07-30 release-owner override authorizing the
initial `0.1.0` production publication and the corrective `0.1.1` atomic
release decision. Historical evidence remains unchanged.

## Implemented evidence

- [x] OWL 2 constructor audit and generated coverage (`reports/conformance/`).
- [x] Python/native differential, security, fuzz, and resource-limit machinery.
- [x] Parse-once consumer/provider/wire handoff at exact commits (`reports/integration/WP11.md`).
- [x] Shared-host performance, mmap, incremental view, and handoff evidence
      (`reports/performance/`).
- [x] Curated public API snapshot audit (`tools/audit/public-api-v0.txt`).
- [x] Migration, compatibility, security, troubleshooting, and executable examples.
- [x] Explicit pure/native build modes, archive/import audits, locked dependency
      inventory, deterministic SBOMs, and fail-closed release report generator.
- [x] Immutable-candidate hosted wheel/release workflow definitions with guarded
      TestPyPI/PyPI promotion; successful execution remains required below.

## Production release authorization

- [x] WP12 artifact resolver matrix passes outside the source tree on Python
      3.10 through the supported upper matrix.
- [x] Pure wheel and forced-pure offline sdist require no compiler or Java and
      contain the complete feature set.
- [x] Approved native wheels pass forced parity, dynamic dependency/tag/ABI,
      sanitizer/fuzz, and platform checks.
- [x] License owner approval is recorded for every linked/bundled third-party obligation;
      NOTICE/source/relinking requirements and SBOM are complete.
- [x] `pyowl-core` publication is authorized through the release owner's
      account-scoped PyPI token; real repository/docs/issues URLs are approved.
- [x] Local token publication replaces the Trusted Publishing rehearsal for
      this release; provenance, yank, rollback, and revocation procedures remain documented.
- [x] The approved DOID comparison and retained native evidence satisfy the
      reference-performance decision for this release.
- [x] Consumer requirements target the intended `>=0.1,<0.2` package line and
      the exact five-consumer compatibility matrix is rerun.
- [x] API/model/wire/adapter versions and the candidate export snapshot receive
      explicit release-owner/consumer-owner approval.

## Corrective 0.1.1 atomic release

- [x] Public PyPI hashes and archive contents prove the existing `0.1.0`
      universal wheel and sdist came from tagged commit `d3e7893`.
- [x] Current native wheels cannot be added to that historical version without
      mixing source revisions; package/native metadata is bumped to `0.1.1`.
- [x] API/model/wire/adapter/encoded-view versions remain unchanged and all
      recorded consumer constraints accept the `0.1.x` line.
- [x] The candidate workflow requires one sdist, one universal wheel, and all
      25 native wheels from one exact source revision.
- [x] Existing paired DOID and retained-native performance evidence is accepted
      because the patch preserves the measured public contracts and semantics.
- [x] TestPyPI and pre-upload signature gates are waived for the authorized
      account-token publication; direct PyPI verification remains required.
- [x] Creation of `v0.1.1` from the final validated release commit is
      authorized.
- [ ] Run CI, native safety, and Wheels at the final `0.1.1` commit; the Wheels
      aggregate must replace the staged advisory and platform-artifact gates.
- [ ] Upload the identical checksum-bound 27-file set and verify its public
      PyPI hashes.

The historical `0.1.0` decision remains under `reports/release/0.1.0/`. The
new source audit, owner authorization, and staged exact-run gates are under
`reports/release/0.1.1/`.
