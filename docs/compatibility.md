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

The coordinated native-redesign compatibility run used exact core runtime
`005c3ccad129757b3a9be125dc064b812b607ef5`, tree
`d4f3f29f6594b59f3d45a4811c38fb761a7028b9`. Its public encoded descriptor is
SHA-256 `9ad29db6a7e616f65cea2957bc5ba8d1f9b99ef0eb1fe1432c09be25786267b5`.
The direct-comparator safety successor is
`a81665241ae86036a3fbe0325f7bcf43660f3a12`; it repeats the native release
profile and makes its build-contract revision a Cargo-tracked input. The final
performance-evidence commit is `4fe32971780e38d2d83932bb93b8c2195bdfcc5f`,
which adds the paired DOID result. The compatibility manifest records that
evidence subject instead of attempting to embed its own circular Git identity.
Neither successor changes the `pyowl_core` package runtime used by consumers.

Every recorded workflow requires public encoded structural schema
`pyowl-core/structural-columns` v1. Compatibility consumers observe that
capability only through public core/reasoner contracts; encoded-native
compilers consume the public buffers through their own private compilers.
The runtime commit is the exact tested source, while the direct-safety and
final commits bind subsequent comparator and performance evidence.

| Consumer | Public role | Tested package | Core range/API | Runtime commit | Final commit |
|---|---|---:|---|---|---|
| Exact-OM | `compatibility-consumer` | `2.0.0` | `pyowl-core>=0.1,<0.2` | `ab4b76644f6ed58894d0920e47de713ba1ffb358` | `74b48779f1a3ca3e85614d50186ecf40a7f6db65` |
| OAEI Bio-ML eval | `compatibility-consumer` | `0.2.0` | `pyowl-core>=0.1,<0.2` | `94713d5068ce78d90f42e7fb100c7631b6490924` | `94713d5068ce78d90f42e7fb100c7631b6490924` |
| pyELK | `encoded-native-compiler` | `0.1.0.dev0` | core API `(0,1)` | `bc75f4be609626f231cdc91af800f52bae46c766` | `70302fcd6abc27d703eeb8f59027fc1392f4709b` |
| pyHermiT | `encoded-native-compiler` | `0.1.0.dev0` | core API `(0,1)` | `f0d4ebb270f3521b848cd2a858761afd66e72ae2` | `af8f7fc669b28dfc15728c84c78f9094787d288b` |
| OWL2Vec* projector | `encoded-native-compiler` | `0.1.0rc1` | `pyowl-core>=0.1,<0.2` | `46b066f698cc790aceae4f8eaf50212934e94708` | `9f19db3de54b7bdffe45498479edadd72af37218` |

The final concise validations were:

- Exact-OM: the nine-file ontology-only closure passed 107 tests with one
  expected native-fallback warning; the focused closure passed 69 tests with
  one warning.
- OAEI: `python -m unittest discover -s tests` ran 238 tests successfully
  with 13 optional skips. Its installed matrix covered 4 formats, 20 owners,
  and 40 reasoner runs with semantic identity preserved.
- pyELK: its release provenance, shared-snapshot handoff, core contract, and
  reasoner contract selection passed 63 tests against the exact core runtime.
- pyHermiT: 53 final-core parity tests, 20 release/workflow tests, 3
  fail-closed cases, and the focused Rust parity selection passed.
- Projector: release tooling and consumer conformance passed 50 tests.

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
