# WP12 — packaging, supply chain, and release machinery

## Goal

Produce compiler-free pure and verified native artifacts, complete license/SBOM/
Java audits, secure publishing, and a release process that supports Python 3.10.

## Read first

`packaging.md`, `native-backend.md`, `security.md`, `verification.md`, WP09/WP10/
WP11 reports, and the native dependency legal decision.

## Depends on

WP09, WP10 and WP11.

## Owned paths

Release-time shared metadata/build files and packaging paths in the manifest.
Coordinate public version/export changes with WP13.

## Deliverables

- Final optional Rust build modes and reproducible sdist/pure/native wheel jobs.
- Python/platform/local-index artifact selection matrix; clean compiler-free
  Python 3.10+ pure installs and forced-native installs.
- Artifact unpack/dynamic-dependency/tag/metadata/side-effect/Java scans.
- Accurate project/third-party licenses, NOTICE/source/relinking obligations,
  dependency inventory, SBOM, advisory and provenance attestations.
- Reserve/confirm PyPI/TestPyPI `pyowl-core`, replace placeholder URLs, configure
  trusted publishing/recovery and TestPyPI rehearsal.
- Release/yank/security rollback procedure and evidence report generator.

## Acceptance

- every built artifact passes tests outside source tree and resolver matrix;
- pure artifact contains all features/no native and needs no compiler/Java;
- native wheels contain expected extension, pass forced parity/audit and never
  mislabel ABI/platform/free-threading support;
- sdist builds offline in forced pure mode and contains complete sources/notices;
- no `.jar`/`.class`/Java dependency/download code anywhere in artifacts/SBOM;
- legal review approves all linked third-party obligations;
- controlled PyPI name and real URLs are documented; and
- signed release report has no unresolved verification/performance blocker.

