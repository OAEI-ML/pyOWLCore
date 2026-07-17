# WP13 — documentation, API stabilization, and 1.0 readiness

## Goal

Turn verified contracts into accurate user/consumer documentation, freeze the
curated API, publish migration guidance, and run the complete 1.0 release gate.

## Read first

All normative specs, WP11/WP12 reports, consumer migration docs, and generated
release evidence.

## Depends on

WP11 and WP12.

## Owned paths

README/changelog/migration/docs/public exports/docs/release tests in manifest.
Do not change model/wire semantics to make examples convenient; route changes
through spec/version control.

## Deliverables

- Curated `pyowl_core` exports and reference docs for complete model, loading,
  policies, views, overlays/composition, indexes, wire, errors and fallback.
- Standalone, Exact-OM provider, pyELK, pyHermiT, projector and OAEI examples
  demonstrating parse once/no Java and secure import choices.
- Performance docs naming pinned corpus/hardware/backend/options; no unsupported
  marketing claims.
- Migration/deprecation/version compatibility tables, troubleshooting native
  warning/pure install, security/import/cache guidance.
- API docs/examples executed on Python 3.10 pure and native lanes; link/schema/
  snippet tests; changelog and limitation/deviation disclosure.
- Final spec implementation traceability and 1.0 checklist.

## Acceptance

- all examples execute against built artifacts without network/Java unless an
  example explicitly configures a secure resolver;
- docs distinguish document/snapshot/view/overlay/composite and structural vs
  reasoner IR correctly;
- public export snapshot/type stubs/API SemVer/model/wire versions approved;
- all master definition-of-done/release blockers checked with linked evidence;
- placeholder names/URLs/claims removed; and
- final release handoff states exact consumer-compatible version ranges.

