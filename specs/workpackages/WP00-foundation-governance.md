# WP00 — foundation, contracts, and governance

## Goal

Create an installable typed Python-3.10 scaffold, frozen public configuration/
error/version contracts, schema-generation discipline, baseline CI, and audits
that prevent architectural or Java-dependency drift.

## Read first

`SPEC.md`, `architecture.md`, `contracts.md`, `security.md`, `packaging.md`, and
the work-package README/manifest.

## Depends on

None.

## Owned paths

The `WP00` paths in `manifest.toml`. Do not implement model constructors,
parsers, snapshots, indexes, or native code owned by later WPs.

## Deliverables

- Finalize `pyproject.toml` for `pyowl-core`, `pyowl_core`, Python >=3.10,
  Apache project source, typed src layout and development tooling.
- Implement stable exception hierarchy, diagnostics, immutable `ParseLimits`,
  `LoadOptions`, enums, version constants, cancellation/progress primitives.
- Add schema/tag code-generation framework that rejects reused/duplicate tags
  and produces deterministic clean diffs.
- Add architecture/import checks, public export snapshot test, Python 3.10 CI,
  hash-seed determinism lane, license/provenance checks, and Java artifact/
  dependency scan.
- Define contribution/security/deviation/report templates and pre-commit/local
  commands without requiring Java or network.
- Keep optional native build wiring a nonfunctional placeholder until WP07;
  pure scaffold installation must work.

## Acceptance

- build/install/import/typecheck/tests succeed on clean Python 3.10 with no
  compiler/Java/network;
- public versions equal spec and all defaults are immutable/secure;
- exception codes round-trip through a test diagnostic representation;
- generated schema tool is deterministic and fails conflicting changes;
- forbidden dependency/import/Java fixture is caught by tests; and
- `reports/workpackages/WP00.md` records the handoff surface for WP01.

