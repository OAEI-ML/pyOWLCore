# Implementation work packages

These briefs turn the specifications into ownership-safe implementation units.
`manifest.toml` is the machine-readable dependency/ownership source. A work
package starts only after all dependencies meet their acceptance gates and
their public handoff contract is tagged in the implementation branch.

## Execution graph

```text
WP00 foundation
  |
  +--> WP01 model/schema
         |
         +--> WP02 Python formats/documents
         |      |
         |      +--> WP03 imports/snapshots
         |               |
         |               +--> WP04 overlays/composition/fingerprints
         |                         |
         |                         +--> WP05 indexes/views
         |                         |      |
         |                         |      +--> WP06 wire/cache
         |                         |      |      |
         |                         |      |      +--> WP07 native foundation
         |                         |      |               |
         |                         |      +---------------+--> WP08 native features
         |                         |
         +-------------------------+--> WP09 conformance/security

WP05 + WP06 + WP08 --> WP10 performance
WP05 + WP06 + WP08 + WP09 --> WP11 consumer integration
WP09 + WP10 + WP11 --> WP12 packaging/release
WP11 + WP12 --> WP13 docs/API stabilization
```

Security, limits, typing, deterministic behavior, pure/native parity and Java
prohibition are part of every WP's definition of done, not deferred to WP09 or
WP12. Those packages supply cross-cutting harnesses and final evidence.

## Agent rules

1. Read `SPEC.md`, the focused specs listed in the brief, and dependency
   handoff notes before editing.
2. Edit only owned paths. Shared schema/public files are changed by their owner
   or through an explicit coordinated handoff recorded in the PR.
3. Do not implement a consumer compatibility quirk in shared identity.
4. Never introduce a second OWL model/parser or reasoner-specific IR into core.
5. Land Python contract/tests before native acceleration.
6. No fixture/corpus enters the repo without provenance/license/hash.
7. No skipped/xfail test without a deviation/work item and removal condition.
8. Update manifest/spec/versions in the same change when a contract changes.

## Handoff artifact

Each completed WP produces `reports/workpackages/<id>.md` containing commit,
versions, owned-file inventory, tests/matrices, benchmark/security evidence,
deviations, remaining risks, and exact contracts exposed to dependents.

