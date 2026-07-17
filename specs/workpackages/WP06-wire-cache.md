# WP06 — stable wire format, mmap, and cache publication

## Goal

Implement deterministic PYOCORE wire v1, safe decode/mmap, atomic content-
addressed cache publication, and an independent reference implementation.

## Read first

`wire-format.md`, `security.md`, `snapshots-overlays.md`, `model.md`, and WP04/
WP05 handoffs.

## Depends on

WP04 and WP05.

## Owned paths

Wire/schema/reference/cache/fuzz paths in the manifest. Do not serialize
consumer IR, native layouts, pickle, or a stable overlay edit tree.

## Deliverables

- Frozen `schemas/wire-v1.toml`, generated tag/layout code, exact 96-byte header,
  directory/required sections and golden fixtures.
- Canonical Python encoder/decoder with structural validation, independent test
  reader/encoder, version/optional section policy.
- `open_snapshot` mmap ownership/lifecycle and safe lazy model/index access.
- `write_snapshot` atomic/durable/content-addressed cache, locks/leases/GC and
  corruption rebuild/quarantine facade.
- Bounds/reference/digest/order/schema checks before allocation; cancellation,
  limits and every corruption family/fuzzer.

## Acceptance

- encode-decode-encode bytes identical across equivalent views/hash seeds;
- independent reader/encoder and every constructor/import/composition goldens;
- unknown version/required/optional semantics and cache rebuild behavior;
- truncation at every byte plus systematic offsets/counts/tags/references;
- mmap close/replace/fork/concurrent writer/crash-injection safety;
- no pickle/native pointer/path credentials and stable memory gate; and
- WP07/consumer IPC receives frozen v1 schema and public facade.

