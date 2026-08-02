# Specification and implementation traceability

This table connects each normative contract to its implementation work package
and retained evidence. “Implemented” means the repository-owned checks pass; it
does not override an external release blocker in the final column.

The [specification index](../specs/README.md) and
[master specification](../specs/SPEC.md) define precedence and the complete
definition of done.

| Contract | Implementation | Evidence | Release state |
|---|---|---|---|
| [Architecture](../specs/architecture.md) and [public contracts](../specs/contracts.md) | WP00 foundation, layering, diagnostics, versions, limits and audits | [WP00 handoff](../reports/workpackages/WP00.md) | Implemented |
| [Complete structural model](../specs/model.md) | WP01 constructors, schema tags, canonical identity, visitors and validation | [WP01 handoff](../reports/workpackages/WP01.md), [constructor coverage](../reports/conformance/constructor-coverage.json) | Implemented |
| [Parsing and imports](../specs/parsing-imports.md) | WP02 formats/documents and WP03 resolvers/import closures | [WP02 handoff](../reports/workpackages/WP02.md), [WP03 handoff](../reports/workpackages/WP03.md) | Implemented |
| [Snapshots and overlays](../specs/snapshots-overlays.md) | WP04 deltas, persistent overlays, zero-copy composition and fingerprints | [WP04 handoff](../reports/workpackages/WP04.md) | Implemented |
| [Indexes and views](../specs/indexes-views.md) | WP05 lazy structural indexes, overlay patching and composite merging | [WP05 handoff](../reports/workpackages/WP05.md) | Implemented |
| [Wire and cache](../specs/wire-format.md) | WP06 canonical wire, validation, mmap and atomic cache | [WP06 handoff](../reports/workpackages/WP06.md) | Implemented |
| [Native backend](../specs/native-backend.md) | WP07 private Rust boundary and WP08 native Functional/wire/index acceleration | [WP07 handoff](../reports/workpackages/WP07.md), [WP08 handoff](../reports/workpackages/WP08.md) | Implemented capabilities; release platform artifacts remain WP12-gated |
| [Security](../specs/security.md) and [verification](../specs/verification.md) | WP09 conformance, hostile-input, differential, fuzz and security controls | [WP09 handoff](../reports/workpackages/WP09.md), [security matrix](../reports/security/control-matrix.json) | Repository gates implemented; continuous sanitizer and accountable disclosure contact remain release gates |
| [Performance](../specs/performance.md) | WP10 reproducible phase harness, regression comparator and shared-host evidence | [WP10 handoff](../reports/workpackages/WP10.md), [limitations](../reports/performance/limitations.md) | Shared-host gates pass; approved reference-machine and strict full biomedical-pair evidence remain blocked |
| [Consumer adapters](../specs/adapters.md) | WP11 negotiation/cache contracts and exact five-consumer zero-reparse matrix | [WP11 evidence](../reports/integration/WP11.md), [compatibility manifest](../reports/integration/consumer-compatibility.json) | Tested for the exact recorded commits and `0.1` core range only |
| [Packaging and release](../specs/packaging.md) | WP12 artifact/build/resolver/license/SBOM/provenance/publishing machinery | [WP12 handoff](../reports/workpackages/WP12.md), generated evidence under `reports/release/<version>/` | Repository machinery/local artifact evidence implemented; hosted platforms, publication, name/URL control, legal approval, signing and reference-platform success remain blocked |
| Documentation and API stabilization | WP13 guidance plus the WP23 package/API/schema migration | [WP13 handoff](../reports/workpackages/WP13.md), [API guide](api.md), [compatibility](compatibility.md), [release checklist](release-checklist.md) | 0.2.0 contracts documented; final release evidence remains fail-closed |
| [Native ontology redesign](../specs/native-ontology-redesign.md) | WP14 comparator fence, WP15 retained arena/publication, WP16 four-format ingestion, WP17 encoded views/wire, and WP18 integration decisions | [WP14](../reports/workpackages/WP14.md), [WP15](../reports/workpackages/WP15.md), [WP16](../reports/workpackages/WP16.md), [WP17](../reports/workpackages/WP17.md), and [WP18](../reports/workpackages/WP18.md) handoffs | Non-long implementation exists; final native artifact, approved corpus/comparator, platform, and workspace decisions remain open |
| [Large-document reliability](../specs/large-document-reliability.md) | WP19 structured limits, WP20 mapping evidence, WP21 reification evidence, WP22 diagnostic partial mode, and WP23 component canonicalization/schema 2 | [WP20](../reports/workpackages/WP20.md), [WP21](../reports/workpackages/WP21.md), [WP22](../reports/workpackages/WP22.md), and the [WP23 contract](../specs/workpackages/WP23-component-anonymous-canonicalization-v2.md) | Implementation and generated scaling pass; NCIt observation exists, while final-artifact alpha/RSS retention, FMA, full reference comparison, and release artifacts remain gated |

The [reference ledger](../specs/references.md) separates standards authorities
from differential implementations. A passing external comparator never
overrides a W3C or core contract, and Java comparators remain isolated
development evidence rather than runtime or release dependencies.

## Version decision

The active decision ledger is
`schemas/version-decision-v2.toml`: package `0.2.0`, API `(0,2)`, model schema
`2`, wire `(1,2)`, adapter protocol `1`, and encoded structural schema `2`.
WP23 increments model identity for component-scoped anonymous canonicalization,
increments the API minor for structured diagnostics/options, and adds the
optional schema-2 wire section. Adapter protocol remains unchanged. Schema-1
model/cache data and encoded columns are never reinterpreted as schema 2.

Every exact consumer result currently recorded in the repository constrains
core to `>=0.1,<0.2` or API `(0,1)`. Those results remain historical. Package
`0.2.0` cannot close its consumer gate until the ranges/capabilities are updated
and the exact-commit matrix is rerun; changing documentation does not establish
compatibility.

## Open release blockers

The authoritative checklist is the [0.2.0 release checklist](release-checklist.md)
and its machine-readable gate ledger. Final pinned-corpus/native performance,
consumer compatibility, complete hosted artifacts, advisory/platform audits,
legal/owner approval, tag identity, TestPyPI, publishing identity, and
signature evidence remain blocked until attached to the exact candidate. No
historical `0.1.x` run or local implementation test substitutes for them.
In particular, local automation cannot prove PyPI/TestPyPI control, legal
approval, trusted publication, external signatures, an approved reference machine,
or consumer-owner approval.
