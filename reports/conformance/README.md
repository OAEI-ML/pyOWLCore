# WP09 conformance evidence

This directory contains deterministic, reviewable evidence generated from the
repository. `corpus-lock.json` pins every retained fixture,
`constructor-coverage.json` maps all model constructors to verification
branches, and `summary.json` records the cross-syntax/independent-wire result,
errata decisions, external-oracle policy, and deviation count.

Regenerate or check the evidence without network access or Java:

```console
python -m tools.corpus.manifest --check
python -m tools.corpus.coverage --check
python -m tools.corpus.report --check
```

RDFLib is an exact-version, development-only optional comparator. The normal
lane is authoritative from W3C rules plus independent local encoders and has no
third-party runtime requirement.
