# pyowl-core 0.2.0 release ledger

Status: owner-authorized candidate; exact-source Wheels evidence staged

This directory is the fail-closed release boundary for package `0.2.0`. The
frozen public values are:

| Domain | Value |
|---|---:|
| Package | `0.2.0` |
| API | `(0,2)` |
| Model schema | `2` |
| Wire | `(1,2)` |
| Adapter protocol | `1` |
| Encoded structural schema | `pyowl-core/structural-columns` v2 |

`schemas/version-decision-v2.toml` is the generated contract decision.
`gates.json` is the reviewed input to the checksum-bound release report. The
release owner closes ten policy/acceptance gates in
`owner-release-authorization.md`. `advisory_scan` and
`platform_artifact_audit` remain blocked until the exact-source Wheels
aggregate replaces them with checksum-bound evidence for the immutable
candidate.

Focused implementation tests are not presented as hosted-wheel, advisory, or
platform evidence. Unavailable licensed SNOMED inputs remain `not-run` and
block only the private incident claim. For this release, the owner accepts the
installed-native/py-horned DOID common-contract comparison and its disclosed
limitations as sufficient; the fuller reference-host claim remains unmade.

Files under `reports/release/0.1.0/` and `reports/release/0.1.1/` remain
historical. Their owner decisions, consumer ranges, performance observations,
and artifact results are not relabelled or inherited as `0.2.0` passes.
