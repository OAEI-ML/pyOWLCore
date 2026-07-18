# Version and consumer compatibility

## Independent version domains

| Domain | Current | Compatibility rule |
|---|---:|---|
| Package/API | `0.1.0.dev0`, API `(0,1)` | SemVer and explicit API tuple |
| Model | `1` | equality/fingerprint changes require a new schema |
| Wire | `(1,1)` | major incompatible; minor only backwards-compatible additions |
| Adapter | `1` | provider/plugin negotiation must match |

## Tested workspace consumers

The WP11 compatibility run tested these exact ranges and commits:

| Consumer | Tested package | Core range/API | Exact commit |
|---|---:|---|---|
| Exact-OM | `2.0.0` | `pyowl-core>=0.1,<0.2` | `e943881befd7673f42b4b4b0b9230b47364c8f35` |
| OAEI Bio-ML eval | `0.2.0` | `pyowl-core>=0.1,<0.2` | `1a6e2e5533cd24af6852fd7ae6029d0f7cd010fa` |
| pyELK | `0.1.0.dev0` | core API `(0,1)` | `d2ec1e1485180388c93f11e0bdaf4afbdd66583f` |
| pyHermiT | `0.1.0.dev0` | core API `(0,1)` | `77887fd42e4b38586ee860a36678ac57bf689071` |
| OWL2Vec* projector | `0.1.0rc1` | `pyowl-core>=0.1,<0.2` | `490eeb89d450723cf8933913e2ccfa53d6fe4140` |

The machine-readable authority is
[`reports/integration/consumer-compatibility.json`](../reports/integration/consumer-compatibility.json).
Do not infer compatibility with later untested commits or widen these ranges
from model/wire numbers alone.

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
