# 1.0 readiness and release checklist

This ledger records the 2026-07-30 release-owner override authorizing the
initial `0.1.0` production publication. Historical evidence remains unchanged;
the override closes the remaining external gates without relabeling historical
local runs.

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

The exact owner decision and per-gate disposition are recorded under
`reports/release/0.1.0/`.
