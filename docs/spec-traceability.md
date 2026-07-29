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
| Documentation and API stabilization | WP13 curated guidance, executable examples, API snapshot and release tests | [WP13 handoff](../reports/workpackages/WP13.md), [API guide](api.md), [compatibility](compatibility.md), [release checklist](release-checklist.md) | 0.1 candidate stabilized; 1.0 remains subject to WP12 plus explicit release-owner and consumer-owner approval |
| [Native ontology redesign](../specs/native-ontology-redesign.md) | Planned WP14 symmetric comparator fence, WP15 versioned builder→snapshot handoff, parallel WP16 ingestion and WP17 views/wire, then WP18 core/workspace decisions | No successor handoff exists yet; the [work-package plan](../specs/workpackages/README.md) is prospective | Specified, not implemented; current native capabilities satisfy neither retained-native/Horned core release gates nor the separate workspace optimization claim |

The [reference ledger](../specs/references.md) separates standards authorities
from differential implementations. A passing external comparator never
overrides a W3C or core contract, and Java comparators remain isolated
development evidence rather than runtime or release dependencies.

## Version decision

No WP13 implementation change alters equality, constructor semantics, wire
meaning, or provider negotiation. The candidate therefore remains package
`0.1.0.dev0`, API `(0,1)`, model schema `1`, wire `(1,1)`, and adapter protocol
`1`. Moving the package to `1.0.0` is a coordinated release decision, not a
documentation edit: every existing consumer currently constrains core to
`>=0.1,<0.2` and must be retested after its range changes.

The WP14-WP18 redesign specification likewise does not claim an implementation
or increment those constants. WP17 exclusively records and applies the API,
adapter-protocol, and encoded-schema decision before advertising
`EncodedStructuralView`; retained private storage alone does not change model
or wire meaning. WP18 exclusively applies the later package SemVer bump and
release changelog/migration metadata without reinterpreting WP17's ledger.

## Open release blockers

The authoritative checklist is [1.0 readiness](release-checklist.md). In
particular, local automation cannot prove PyPI/TestPyPI control, legal approval,
private recovery ownership, trusted publication, external signatures, an
approved reference machine, or compatibility of commits that were not tested.
Those items stay unchecked until their evidence exists.
