# WP14 retained-storage and API decision

Status: frozen input to WP15–WP17; it does not advertise a runtime capability.

- Native documents and snapshots own private opaque handles backed by immutable
  shared Rust storage. Public contracts never expose Rust types, pointers, enum
  discriminants, or arena IDs.
- Native-owned buffers are retained by strong owner references. Mapped storage
  retains the validated mapping owner; close/fork behavior must fail safely.
- Scalar Python entities, expressions, and axioms are materialized lazily and
  compare/hash exactly like Python-created model values. Caches are bounded or
  weak so a scan cannot permanently recreate the whole ontology in Python.
- Bulk consumers use the additive, versioned `EncodedStructuralView` direction
  in `indexes-views.md`. Its little-endian schema is independent of native
  layout, contains a strong owner, and has an identical Python fallback.
- The stable PYOCORE wire remains the only cross-process/cache representation.
  Native tables may align with wire/view columns after measurement, but native
  memory is never reinterpreted as a public format.
- `open_snapshot(mmap=True)` validates before publication and must not create a
  Python object or copied native row for every ontology item.
- Backend capabilities are additive and fail closed. The authoritative names
  are `retained-native-load-v1`, `ontology-identity-index`, `wire-v1`,
  `wire-verified`, and
  `encoded_view_schemas["pyowl-core/structural-columns"]`. The retained-load
  feature and encoded-schema entry remain absent until their complete forced
  installed-artifact matrices pass. `wire-v1` and `wire-verified` describe a
  successfully decoded/mapped PYOCORE view, not a direct parsed view or a
  generic mmap capability. `AUTO` selects Python before consuming input when a
  required native capability is absent; forced native raises.
- WP17 records the encoded schema/API/adapter decision. WP18 alone changes the
  package version. WP14 therefore changes no version constant or public export.

The publication boundary is one immutable handle plus bounded document/report
metadata. Native-to-Python full reconstruction, wire encode/decode as an
in-process handoff, and consumer access to private core storage are rejected.
Retained-handle/facade publication is **not applicable** to the pure-Python
benchmark lane; completion of its ordinary Python snapshot remains timed, but
must not be relabeled as native publication.
