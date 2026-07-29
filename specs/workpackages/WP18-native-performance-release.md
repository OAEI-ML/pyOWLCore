# WP18 — native integration, optimization, and comparative release gate

## Goal

Integrate the retained native ingestion and bulk-view paths, remove measured
bottlenecks, prove core release behavior, and meet the Horned-equivalence
minimum while attempting to outperform every comparator. Track the stronger
workspace-wide consumer optimization as a separate completion decision.

## Read first

All focused specifications, especially `native-ontology-redesign.md`,
`performance.md`, `verification.md`, `security.md`, and `packaging.md`, plus the
WP12/WP13 release-stabilization and WP14/WP16/WP17 handoffs.

## Depends on

WP12, WP16 and WP17.

## Owned paths

Comparative/consumer performance harnesses, native-redesign reports, packaging/
release tooling and workflows, dependency/license inventories, public release
and performance documentation, and cross-cutting acceptance tests listed in the
manifest. This is an explicit successor handoff from WP12/WP13. Profile-driven
changes to WP15-WP17 implementation paths require an explicit ownership handoff
and retain their originating parity tests.

## Deliverables

- End-to-end installed-wheel integration across formats, imports, direct/mmap
  snapshots, indexes, overlays/composites, the independent encoded-view
  decoder, and current scalar-consumer compatibility.
- A separately statused workspace matrix across the exact pyELK, pyHermiT,
  projector, Exact, and OAEI successor revisions as those artifacts become
  available. Missing external revisions are `not-run`, never fabricated passes
  or hidden core blockers.
- Profiles followed by evidence-backed algorithm/layout/copy reductions and
  bounded parallelism; unsafe/vectorized work only under native policy.
- Fresh-process and steady-process direct-engine and delivered-wheel comparisons
  against pinned Horned-OWL/py-horned and isolated OWLAPI, with exact output
  inventories and raw samples.
- Wheel/platform/Python 3.10+ performance, memory, ABI, sanitizer/fuzz, SBOM,
  license, Java-prohibition, pure-fallback, and clean-install evidence.
- The single package SemVer release decision applied consistently to
  `pyproject.toml` and `pyowl_core.__version__`, followed by changelog/migration
  and release documentation. WP17's API/adapter/encoded-schema ledger is an
  immutable input to this step, not a second version decision.
- Two separately reported decisions: `core_release_eligible` for core-owned
  correctness/loading/packaging gates and `workspace_optimization_complete`
  for the exact companion-revision multi-consumer performance matrix.
- Updated limitations and user performance documentation that states only
  demonstrated corpus/hardware/backend results.

## Acceptance

- All semantic, differential, security, resource, determinism, and packaging
  gates pass before any comparative sample is accepted. Core-owned independent
  decoder/current-consumer compatibility gates are required for
  `core_release_eligible`; unavailable successor consumer revisions remain
  explicit `not-run` entries rather than blocking that decision.
- Retained-native query-ready wall time and incremental peak RSS pass the
  aggregate confidence bounds and per-large-corpus Horned guardrails in
  `performance.md` for both required modes.
- The installed wheel stays within its native-engine facade overhead budget and
  proves no eager Python expansion or per-axiom Python consumer callback.
- Results against OWLAPI and every other pinned comparator are reported; the
  stretch claim is made only if pyowl-core actually beats them under the common
  gate.
- mmap/repeated-run and multi-consumer workflows demonstrate the intended
  shared-snapshot advantage without hiding cache preparation in the timer.
- `workspace_optimization_complete` is true only after the exact pyELK,
  pyHermiT, projector, Exact, and OAEI revisions pass that workflow. If it is
  false, the core may still release when `core_release_eligible` is true, but
  multi-consumer native-performance claims remain prohibited.
- WP18 may close as a core work package with
  `core_release_eligible=true` and
  `workspace_optimization_complete=false` when the remaining entries are
  unavailable external revisions recorded as `not-run`. That open workspace
  status is carried by the coordinated companion milestone, not disguised as
  incomplete core correctness or a failed Horned loading gate.
- Package `__version__` and project metadata agree; API, adapter, model, wire,
  and encoded-view schema constants exactly match WP17's ledger.
- Installed-wheel capability reports are recomputed from the complete
  forced-native integration matrix, including negative/error and retained
  cross-layer regression fixtures; passing isolated parser/view unit tests
  cannot advertise a format or encoded schema.
- Pure wheels remain fully functional without a compiler, and all distributed
  artifacts remain Java-free and accurately licensed.
