# pyowl-core 0.1.1 release-owner authorization

Date: 2026-07-30

The release owner authorized a corrective `0.1.1` production release after the
public `0.1.0` universal wheel and sdist were proven to originate from the
older `v0.1.0` source revision. Native wheels built from the corrected source
must not be added to that historical file set.

This authorization preserves every file under `reports/release/0.1.0/` as
historical evidence. The release owner explicitly:

- accepts the existing paired DOID and retained-native performance evidence
  because `0.1.1` does not change the measured public contracts or semantics;
- authorizes creation of `v0.1.1` from the final validated release commit;
- waives TestPyPI rehearsal and pre-upload signature gating for direct
  account-scoped PyPI API-token publication, followed by public-index
  verification; and
- closes the legal review as approved, including the explicit decision to
  ignore `LIC-001` for this release.

Only exact-source advisory and platform-artifact evidence remains staged in
`gates.json`; the final Wheels run must replace those two entries. The protected
OIDC/TestPyPI workflow remains configured as the stronger path for future
releases and may replace the owner-waiver evidence when used.

All 27 `0.1.1` files—one sdist, one universal wheel, and 25 native wheels—must
be built and promoted as one checksum-bound candidate from one Git revision.
API `(0,1)`, model schema `1`, wire `(1,1)`, adapter protocol `1`, and encoded
structural view v1 remain unchanged.
