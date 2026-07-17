# Packaging, installation, and release

## 1. Identity and supported runtime

- distribution: `pyowl-core` (provisional until reservation gate passes)
- import: `pyowl_core`
- Python: `>=3.10`
- project source license: Apache-2.0
- typed package: `py.typed`
- no Java runtime/build dependency or artifact

The supported upper Python version is the newest stable version exercised by
release CI; metadata must not cap Python without evidence. Python 3.10 remains
in every semantic/packaging lane until a future major release explicitly drops
it. CPython and PyPy run the pure backend; native platforms are listed only when
their wheels pass the full matrix.

## 2. Source layout and metadata

Use `src/` layout and PEP 517/518 metadata in `pyproject.toml`. A minimal
`setup.py` may exist solely to configure the optional Rust extension/build mode;
it must not duplicate project metadata or import the package during build.

The sdist contains:

- Python source, typing markers/stubs, Rust source and `Cargo.lock`;
- build configuration, schema/tag ledgers and code generators;
- LICENSE, NOTICE, README, specifications and third-party license inventory;
- required small test/golden fixtures with provenance; and
- no build products, downloaded corpora, credentials, JVM/class/jar artifacts,
  platform binaries, or mutable VCS metadata.

Generated source is reproducible from checked-in schema plus pinned tools;
release CI regenerates and requires a clean diff.

## 3. Artifact set

For every version publish to one index release:

1. one `py3-none-any` complete pure-Python wheel;
2. native CPython wheels for approved Python/platform/architecture combinations;
3. one sdist from which native or pure builds can be selected; and
4. checksums, signatures/provenance attestations and SBOMs.

Initial native wheels are Python-version-specific. `abi3` is not claimed until
its exact minimum version and buffer/subinterpreter/free-threaded behavior pass
review. Unsupported CPython/PyPy/platform resolves to the pure wheel, not an
installation error. Resolver tests use a local index containing every artifact
simultaneously and prove selection on all targets.

Official native artifacts set `PYOWL_CORE_BUILD_NATIVE=1`; pure artifacts set
it to `0`. Local sdist default `auto` may fall back with build output explaining
why. An official job never accepts optional native compilation failure.

## 4. Dependency policy

Base runtime is minimal, Python-3.10-compatible, and cannot depend on
`py-horned-owl` merely as a public object bridge. Optional parser/resolver
dependencies use extras/plugins and cannot change default behavior when merely
installed.

Every dependency has:

- purpose and why stdlib/current dependencies are insufficient;
- minimum/maximum policy justified by compatibility/security;
- license and transitive license review;
- maintained-release/advisory evidence;
- import/startup/installed-size impact; and
- Java/JVM/native dynamic dependency scan.

Native use of Horned-OWL is release-blocked on legal review. If linked, wheel/
sdist metadata, NOTICE, source availability and any relinking obligations must
accurately reflect LGPL/transitive terms. Otherwise use the reviewed clean-room
implementation. It is forbidden to label a multi-license linked artifact as
solely Apache-2.0.

## 5. Compiler-free installation

From an environment with no Rust/C compiler and no Java:

```text
pip install pyowl-core
python -c "import pyowl_core; ... parse required formats ..."
```

must install a pure wheel and pass complete functional tests. Building the
sdist in `PYOWL_CORE_BUILD_NATIVE=0` also succeeds offline with only declared
Python build requirements. `AUTO` compilation failure cannot leave a broken
extension stub or disable features; runtime emits the specified once/process
warning when it selects Python.

## 6. Wheel/platform matrix

At minimum evaluate:

- Linux x86_64 and aarch64 using an appropriate manylinux baseline;
- macOS x86_64 and arm64 (universal2 only if independently tested);
- Windows x86_64; and
- pure wheel on CPython 3.10–newest and supported PyPy.

Additional architectures are advertised only after real or official emulated
tests, including large-file mmap and endianness/alignment assumptions. Linux
wheels are repaired/audited; macOS deployment targets and Windows CRT DLLs are
inspected. No wheel downloads code/data at import or first parse.

Free-threaded CPython and subinterpreters use pure Python until thread-safety and
extension tags are fully audited. The dispatcher must proactively avoid an
incompatible extension rather than crash.

## 7. Artifact inspection

CI unpacks every sdist/wheel and asserts:

- metadata name/version/license/Python/tags/extras/URLs are correct;
- only expected package/native/schema/license files occur;
- `RECORD`, executable bits, shared library dependencies, rpaths/install names,
  debug symbols and symbol exports meet policy;
- no `.jar`, `.class`, `.war`, `.ear`, JVM launcher/config, Maven/Gradle cache,
  OWLAPI/ROBOT/JPype dependency, or Java download code/string manifest appears;
- source and binary license/NOTICE obligations are present;
- pure wheel contains no native library and passes all semantic tests;
- native wheel contains exactly the expected extension and passes forced-native
  parity/self-test; and
- package import performs no network, filesystem cache write, plugin import, or
  fallback warning until an auto-accelerated operation is requested.

The Java scan covers archive contents/dependency lockfiles/build scripts/SBOM,
not just filename suffixes. Development-only oracle tooling is excluded from
release artifacts and environments.

## 8. Reproducibility and supply chain

Builds use pinned actions/toolchains/dependencies, least-privilege publishing,
trusted publishing where available, artifact attestations, checksums, SBOM, and
separate build/publish approval. Rust lockfile/MSRV and Python build constraints
are tested from clean network-restricted builders.

Rebuilding identical source/toolchain should produce identical pure wheels and
documented native reproducibility (allowing only enumerated platform metadata).
Timestamps/archive order/file modes are normalized. Signing occurs after build
without mutating artifacts.

## 9. PyPI name and project URLs

`pyowl-core` is provisional. Before the first public release, a release owner
must:

1. query PyPI and TestPyPI using authenticated official workflows;
2. reserve or confirm control of the exact normalized name `pyowl-core`;
3. record owner/organization and recovery contacts privately;
4. replace every `OWNER` placeholder with real repository/docs/issues URLs;
5. test normalized-name collision/confusion (`pyowl_core`, case/hyphens); and
6. publish a signed minimal pre-release only after metadata/legal/security
   approval.

Failure to secure the name blocks release and triggers a reviewed rename across
distribution/docs/workspace specs before implementation consumers publish. No
agent should squat or publish the provisional name autonomously.

## 10. Release process

1. Freeze API/model/wire schemas and changelog/migration notes.
2. Run verification/security/performance/consumer matrices from a signed tag.
3. Build once in isolated builders, inspect/test artifacts without source tree.
4. Run local-index resolver and compiler-free/forced-native installations.
5. Complete license/SBOM/advisory/Java/reproducibility/name gates.
6. Publish pre-release to TestPyPI; repeat installation/consumer smoke tests.
7. Approve and promote the already-built artifacts to PyPI.
8. Verify index metadata/signatures/install, then publish docs/release notes.

Yanking/revocation procedures cover corrupted wheels, semantic divergence,
security issues, name/metadata errors and license omissions. A native-wheel
failure never requires yanking the valid pure artifact if resolver metadata can
be corrected safely, but version immutability is respected.

