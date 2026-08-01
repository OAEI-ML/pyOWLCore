# Implementation work packages

These briefs turn the specifications into ownership-safe implementation units.
`manifest.toml` is the machine-readable dependency/ownership source. A work
package starts only after every dependency has a reviewed repository-owned
handoff contract in the implementation branch. External publication/reference-
machine gates may carry into a successor only where its brief says so; they
remain release blockers and cannot be relabeled as passed.

The manifest's top-level version fields describe the final successor target;
the implementation continues to advertise its released values until the owning
workpackage lands the coordinated version change.

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

Successor retained-native redesign:

WP09 + WP10 + WP13 --> WP14 contract/comparator baseline
WP07 + WP09 + WP14 --> WP15 retained arena/lazy facade
WP02 + WP03 + WP15 --> WP16 native streaming ingestion
WP05 + WP06 + WP11 + WP15 --> WP17 encoded views/wire/consumer handoff
WP12 + WP16 + WP17 --> WP18 comparative performance release

Large-document reliability successor:

WP18 --> WP19 structured limits/telemetry
WP19 --> WP20 strict RDF mapping evidence
WP20 --> WP21 reification evidence
WP21 --> WP22 partial RDF diagnostic API
WP21 --> WP23 component anonymous canonicalization v2
```

WP16 and WP17 are the intended parallel lanes after WP15 freezes the retained-
storage, private-stub, and registration handoff. Their manifest ownership is
disjoint: ingestion and encoded-view bindings/adapters are separate files.
WP18 integrates them and owns comparative release evidence.
WP20 and WP21 intentionally serialize ownership of the Python/native RDF
mapping files. After WP21, WP22 and WP23 are disjoint implementation lanes:
WP22 owns parser option routing and WP23 owns anonymous identity, schemas,
versions, and the large-document gate. WP23 consumes WP22's frozen API handoff
before the final release/version commit even though component implementation
does not wait for it. WP23 remains open until that handoff is incorporated.
WP17 exclusively owns the API/adapter/encoded-schema decision and records it in
a generated ledger; WP18 exclusively owns the later package SemVer bump and
release changelog/migration metadata. Their shared `__init__.py` handoff is
line-scoped: WP17 establishes exports/contract constants, while WP18 changes
only package `__version__` and verifies the frozen ledger.
WP00-WP18 handoffs remain historical evidence; successor ownership reopens only
the paths listed in the manifest and does not retroactively alter old reports.
For WP14-WP18 scheduling, a predecessor's recorded repository-owned handoff is
sufficient to begin implementation; unresolved external publication, legal,
hosted-platform, and reference-machine release evidence carries forward and
must still be closed where required before WP18 can claim the corresponding
`core_release_eligible` or `workspace_optimization_complete` decision.

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
