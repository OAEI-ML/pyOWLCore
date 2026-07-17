# Contributing

Read `specs/SPEC.md`, the relevant work-package brief, and its dependencies
before editing. Work packages own disjoint paths except where the manifest
explicitly schedules later stabilization. Public behavior follows W3C OWL 2
and the repository contracts, not an implementation majority vote.

## Local checks

The dependency-free gate works offline on Python 3.10 or newer:

```console
python tools/check.py
```

Install the `dev` extra and run `python tools/check.py --full` before handoff.
Configure the tracked hook with `git config core.hooksPath .githooks`.

Every work package adds tests and a report based on
`specs/templates/workpackage-report.md`. Schema changes retain retired tag
reservations and regenerate deterministically. External fixtures require exact
provenance. A standards or compatibility deviation uses
`specs/templates/deviation.toml`; undocumented allowlists are not accepted.

Project source contributions are Apache-2.0 unless explicitly documented and
reviewed. Do not add Java artifacts, JVM bridges, reverse consumer imports,
network-dependent unit tests, generated build products, or secrets.
