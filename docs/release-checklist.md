# 1.0 readiness and release checklist

This is a verification ledger, not a statement that external approvals or
publication occurred.

## Implemented evidence

- [x] OWL 2 constructor audit and generated coverage (`reports/conformance/`).
- [x] Python/native differential, security, fuzz, and resource-limit machinery.
- [x] Parse-once consumer/provider/wire handoff at exact commits (`reports/integration/WP11.md`).
- [x] Shared-host performance, mmap, incremental view, and handoff evidence
      (`reports/performance/`).
- [x] Curated public API snapshot audit (`tools/audit/public-api-v0.txt`).
- [x] Migration, compatibility, security, troubleshooting, and executable examples.

## Required before a public release

- [ ] WP12 artifact resolver matrix passes outside the source tree on Python
      3.10 through the supported upper matrix.
- [ ] Pure wheel and forced-pure offline sdist require no compiler or Java and
      contain the complete feature set.
- [ ] Approved native wheels pass forced parity, dynamic dependency/tag/ABI,
      sanitizer/fuzz, and platform checks.
- [ ] License owner approves every linked/bundled third-party obligation;
      NOTICE/source/relinking requirements and SBOM are complete.
- [ ] `pyowl-core` ownership is confirmed on PyPI and TestPyPI, recovery contacts
      are recorded privately, and real repository/docs/issues URLs are approved.
- [ ] Trusted-publishing rehearsal, attestations, signatures, provenance, yank,
      rollback, and security-revocation procedures pass.
- [ ] An approved reference-machine candidate passes the pinned regression and
      large biomedical gates; current shared-host evidence is informative only.
- [ ] Consumer requirements are updated for the intended package version and
      the exact five-consumer compatibility matrix is rerun.
- [ ] API/model/wire/adapter versions and the candidate export snapshot receive
      explicit release-owner/consumer-owner approval.

Any unchecked item is a release blocker. No local test can fabricate index
ownership, legal approval, signatures, or external publishing evidence.

