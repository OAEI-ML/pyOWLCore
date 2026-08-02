# pyowl-core 0.2.0 release ledger

Status: blocked candidate

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
`gates.json` is the reviewed input to the checksum-bound release report. Every
gate starts blocked: a final run may replace a description only with evidence
for the exact selected source revision and immutable candidate.

In particular, focused implementation tests do not close the pinned-corpus,
forced-native, consumer, hosted-wheel, advisory, platform, or publication
gates. Unavailable licensed SNOMED inputs remain `not-run` and block only the
private incident claim. The public release-performance gate still requires its
specified redistributable and pinned-corpus evidence.

Files under `reports/release/0.1.0/` and `reports/release/0.1.1/` remain
historical. Their owner decisions, consumer ranges, performance observations,
and artifact results are not relabelled or inherited as `0.2.0` passes.
