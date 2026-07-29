# WP17 — encoded views, native indexes, wire/mmap, and consumer handoff

## Goal

Make one retained ontology usable at full throughput by Python and independent
native consumer packages, and make canonical wire/mmap the zero-reparse
persistent path.

## Read first

`native-ontology-redesign.md`, `indexes-views.md`, `contracts.md`,
`snapshots-overlays.md`, `wire-format.md`, `adapters.md`, `packaging.md`, and the
WP05/WP06/WP11/WP15 handoffs.

## Depends on

WP05, WP06, WP11 and WP15. It may run in parallel with WP16 against WP15's
frozen retained-storage handoff.

## Owned paths

Generated encoded-view schema, encoded structural view implementation, native
view binding/index/wire/export modules, mapped storage integration, overlay/
composite bulk segments, public API/adapter compatibility and stabilization
files, and native consumer-handoff tests listed in the manifest. WP15's shared
registration/dispatch seam is frozen so this package remains disjoint from
WP16's ingestion lane.

WP17 receives only WP15's published owning snapshot handle/protocol. It attaches
encoded views, indexes, and wire/mmap exporters without constructing import
closures or modifying WP16 acquisition/orchestration. Fixtures run against both
WP15's generated publication fake and WP16-produced real snapshots.

## Deliverables

- Versioned `EncodedStructuralView` schema/descriptor, read-only buffer owner
  and capability negotiation covering every OWL structural constructor.
- Direct native columns and a complete Python fallback producer with identical
  buffer semantics; no consumer dependency on private Rust layout.
- Native common indexes and segmented overlay/composite exports that reuse base
  buffers and express only bounded postings/deltas by default.
- Canonical native wire encoding/validation and `mmap=True` publication as a
  lazy retained view without per-row Python objects or a complete native copy.
- Zero-reparse fixtures for Exact-OM, pyELK, pyHermiT, projector, and OAEI-style
  composition, including a core-owned independent decoder/coarse native-
  consumer test harness. These fixtures freeze the contract; they do not
  implement or claim completion of the three external native compilers.
- Coordinated companion specification/version-range handoffs for those five
  repositories so their implementation lanes can adopt the candidate encoded
  schema in parallel without transferring external-repository ownership into
  this core work package.
- The handoff names and tests pyELK WP14, pyHermiT WP18, projector P7, Exact-OM WP-N, and
  OAEI 0.2.x explicitly; core publishes schema fixtures/counters while each repository retains
  ownership of its compiler or compatibility adapter.
- A generated version-decision ledger that is the sole source for the encoded-
  view schema and any `API_VERSION`/`ADAPTER_PROTOCOL_VERSION` change, plus
  public exports/API audit, typing, compatibility guidance, and consumer range
  coordination before advertising the capability. WP17 applies those contract
  constants but does not change project/package `__version__`,
  `pyproject.toml`, `CHANGELOG.md`, or `MIGRATION.md`.

## Acceptance

- Encoded buffers equal scalar traversal and independent reference decoding for
  every constructor using WP15 generated/native-arena fixtures plus the existing
  Python, wire, mmap, overlay, and composite backends. The WP16 format × WP17
  encoded-view cross-product is an explicit WP18 integration gate, preserving
  genuine wave-15 parallelism.
- The encoded schema is not advertised from unit coverage alone. Installed-
  wheel forced-native tests cross published snapshots, direct/mmap/overlay/
  composite owners, hostile descriptors, negative/errors, and at least one
  public consumer operation with fallback disabled. The retained OAEI/pyHermiT
  multi-level incoherence fixture must reach consumer Python/native parity from
  the same encoded structure even though its reasoning semantics remain
  consumer-owned.
- Native direct/mmap view publication makes no ontology-sized copy; buffer
  lifetime, alignment, endianness, close/fork/concurrency, limits, corruption,
  and hostile descriptors pass.
- Consumer handoff increments no parser, resolver, wire encoder/decoder, scalar
  axiom-materialization, or base-copy counter and preserves the exact public
  `OntologyView` identity.
- Repair composites retain source/target arenas and bridge/delta segments rather
  than flattening bases; results equal explicit materialization.
- The pure-Python scalar and encoded paths remain complete, and static scans
  reject consumer imports of `pyowl_core._native` or private arena modules.
- The version ledger and runtime/API snapshot agree exactly. Package SemVer is
  still the pre-release value; WP18 alone promotes it and cannot alter WP17's
  API/adapter/encoded-schema decision without returning a contract amendment.
