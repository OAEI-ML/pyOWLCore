# Version and consumer compatibility

## Independent version domains

| Domain | Current | Compatibility rule |
|---|---:|---|
| Package/API | `0.1.0.dev0`, API `(0,1)` | SemVer and explicit API tuple |
| Model | `1` | equality/fingerprint changes require a new schema |
| Wire | `(1,1)` | major incompatible; minor only backwards-compatible additions |
| Adapter | `1` | provider/plugin negotiation must match |

## Tested workspace consumers

The coordinated native-redesign compatibility run used runtime implementation
`af9bdb0b9178766b5f15806fb6a2f00b05e00e22`. The later core revision
`15992ca5b19f795da7870ec183727100758b08d9` changes only the pinned pure-package
CI image, release provenance, benchmark evidence validation, and their checks;
it does not change runtime sources.

These exact consumer revisions passed the recorded short compatibility selections:

| Consumer | Tested package | Core range/API | Exact commit |
|---|---:|---|---|
| Exact-OM | `2.0.0` | `pyowl-core>=0.1,<0.2` | `d172cfa355a5d2683fc47824a5d8f2ed24cf9125` |
| OAEI Bio-ML eval | `0.2.0` | `pyowl-core>=0.1,<0.2` | `04573c09dd0e62825c3fa7c5b2490b43d5a22874` |
| pyELK | `0.1.0.dev0` | core API `(0,1)` | `a909cfcea341834ab6d6598f80445a697b338f13` |
| pyHermiT | `0.1.0.dev0` | core API `(0,1)` | `04bd8163b532f623044d7391706ff728d1aed4b1` |
| OWL2Vec* projector | `0.1.0rc1` | `pyowl-core>=0.1,<0.2` | `53a23e2d385696e2be042568ade0d178580c6de4` |

The machine-readable authority is
[`reports/integration/consumer-compatibility.json`](../reports/integration/consumer-compatibility.json).
Do not infer compatibility with later untested commits or widen these ranges
from model/wire numbers alone. These short semantic and packaging selections
do not stand in for the hosted wheel, long-running performance, fuzz,
sanitizer, licensed-corpus, signing, or external approval gates.

## Interpreter support and evidence

`Requires-Python: >=3.10` is the installation contract; the classifiers name
the CPython versions targeted by the current candidate. Evidence is narrower
than metadata until the hosted WP12 matrix succeeds for the selected revision:

| Interpreter | Candidate policy | Evidence at this handoff |
|---|---|---|
| CPython 3.10 | pure and approved native wheels | complete local pure suite; local macOS x86_64 native wheel/examples; hosted platform matrix pending |
| CPython 3.11 | pure and approved native wheels | workflow lane defined; selected-revision hosted result pending |
| CPython 3.12 | pure and approved native wheels | complete local pure/source-tree suite; hosted native matrix pending |
| CPython 3.13 | pure and approved native wheels | workflow lane defined; selected-revision hosted result pending |
| CPython 3.14 | pure and approved native wheels | workflow lane defined; selected-revision hosted result pending |
| PyPy 3.10 | pure wheel only | resolver/test lane defined; selected-revision hosted result pending |

“Supported” therefore does not mean that an unexecuted native wheel already
exists. Unsupported implementations/platforms must resolve to the universal
pure wheel, and native support is advertised only after its target lane passes.

## 1.0 handoff rule

The current consumers exclude package version 1.0. A 1.0 release therefore
requires coordinated consumer dependency updates and a repeated exact-commit
matrix. Until that evidence exists, the 1.0 API snapshot is a candidate and the
release checklist remains blocked; documentation does not silently relabel the
tested 0.1 line as consumer-compatible 1.0.
