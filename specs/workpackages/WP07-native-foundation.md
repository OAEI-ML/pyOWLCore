# WP07 — native ABI, canonical model, and wire foundation

## Goal

Create the private Rust/PyO3 extension boundary, safe owned buffers, panic/
cancel/resource behavior, and model/canonical/wire parity before accelerating
formats or indexes.

## Read first

`native-backend.md`, `wire-format.md`, `security.md`, `packaging.md`, WP01/WP06
handoffs, and the recorded Horned-OWL legal/capability decision.

## Depends on

WP01 and WP06.

## Owned paths

Native foundation, private stub, dispatch/native adapter and tests listed in the
manifest. Do not modify Python semantics or implement partial parser fallback.

## Deliverables

- Rust workspace/lock/MSRV/features/audits and private `_native` version/self-
  test/error/cancellation boundary.
- Safe private model arena/canonical primitives and complete wire validate/
  encode/decode parity for accepted core views.
- Dispatcher with exact AUTO/PYTHON/NATIVE behavior and once/process warning.
- GIL release/lifetime/panic/fork/subinterpreter/free-threading policy probes.
- Build hooks sufficient for developer forced/pure modes; release artifact
  matrix remains WP12.

## Acceptance

- forced-native empty/every-model/wire goldens byte/fingerprint parity;
- no borrowed buffer escape, panic crossing, abort, unchecked allocation or
  unsafe without approved evidence;
- signals/cancel/deadlines/error codes and sanitizer/Miri/fuzz foundations;
- incompatible/missing native selects Python before work in AUTO and forced
  native raises;
- pure backend does not import native/Horned code; and
- license review chooses clean-room or compliant multi-license path before any
  third-party native linkage advances.

