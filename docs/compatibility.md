# Version and consumer compatibility

## Independent version domains

| Domain | Current | Compatibility rule |
|---|---:|---|
| Package/API | `0.1.0.dev0`, API `(0,1)` | SemVer and explicit API tuple |
| Model | `1` | equality/fingerprint changes require a new schema |
| Wire | `(1,1)` | major incompatible; minor only backwards-compatible additions |
| Adapter | `1` | provider/plugin negotiation must match |
| Encoded structural view | `pyowl-core/structural-columns` v1 | consumers require the named schema at version 1 or newer |

## Tested workspace consumers

The coordinated native-redesign compatibility run used core runtime
`21503cf5a35c22c1fa35653c13df958df4fca100`. The final recorded core tree is
`9251059e10ab1c4474d58d7c3d61b63c0ae3d23c`; its later commits change consumer
and release/benchmark evidence, not the `pyowl_core` package runtime.

Every recorded workflow requires public encoded structural schema
`pyowl-core/structural-columns` v1. Compatibility consumers observe that
capability only through public core/reasoner contracts; encoded-native
compilers consume the public buffers through their own private compilers.
The runtime commit is the last package-runtime change, while the final commit
also binds subsequent tests, documentation, and release evidence.

| Consumer | Public role | Tested package | Core range/API | Runtime commit | Final commit |
|---|---|---:|---|---|---|
| Exact-OM | `compatibility-consumer` | `2.0.0` | `pyowl-core>=0.1,<0.2` | `ab4b76644f6ed58894d0920e47de713ba1ffb358` | `abba717bd5b3f186678bd6f3e88bf73066c2ae49` |
| OAEI Bio-ML eval | `compatibility-consumer` | `0.2.0` | `pyowl-core>=0.1,<0.2` | `fd75aedbf9f5ed4351d3f6d634a6e07721d21778` | `e5d1affaf66600b09b8d771c2bb691a10cfda852` |
| pyELK | `encoded-native-compiler` | `0.1.0.dev0` | core API `(0,1)` | `bc75f4be609626f231cdc91af800f52bae46c766` | `faf7a995bd4b44964d7e5a56007ae484df79d597` |
| pyHermiT | `encoded-native-compiler` | `0.1.0.dev0` | core API `(0,1)` | `f0d4ebb270f3521b848cd2a858761afd66e72ae2` | `f0d4ebb270f3521b848cd2a858761afd66e72ae2` |
| OWL2Vec* projector | `encoded-native-compiler` | `0.1.0rc1` | `pyowl-core>=0.1,<0.2` | `46b066f698cc790aceae4f8eaf50212934e94708` | `8f599fb00708703f3bdbdbbf2d0064bc2935167c` |

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
