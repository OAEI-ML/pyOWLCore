# pyowl-core

[![PyPI](https://img.shields.io/pypi/v/pyowl-core)](https://pypi.org/project/pyowl-core/)
[![Python](https://img.shields.io/pypi/pyversions/pyowl-core)](https://pypi.org/project/pyowl-core/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

`pyowl-core` is a Java-free OWL 2 structural kernel for Python 3.10 and newer.
It parses an ontology once and exposes the same immutable view to Exact-OM,
pyELK, pyHermiT, pyOwl2Vec-Star-projector, and OAEI evaluation code. Consumers
build their own reasoning or projection IR; they do not reparse paths or own a
second OWL object model.

Version `0.1.0` is the first production publication of the stabilized 0.1 API.
The portable implementation is the compatibility baseline; native wheels are
optional accelerators and can be published independently as their platform
builds complete. Release decisions and retained limitations are recorded in
[the release checklist](docs/release-checklist.md).

## Installation

```bash
python -m pip install pyowl-core
```

The portable wheel provides the complete Python implementation without Java or
a native compiler. Compatible platform wheels may add private Rust
acceleration, but public behavior and values remain identical. Use
`BackendPreference.PYTHON` for a quiet, explicit portable path or
`BackendPreference.NATIVE` when acceleration is required and fallback would be
an error.

## Quick start

```python
from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    load_snapshot,
)

source = b"Ontology(<urn:example> Declaration(Class(<urn:example#A>)))"
snapshot = load_snapshot(
    source,
    document_iri="urn:example:document",
    options=LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    ),
)

assert snapshot.is_complete
assert len(tuple(snapshot.iter_axioms())) == 1
```

Passing `snapshot` to another package is an identity-preserving handoff:
`coerce_snapshot(snapshot) is snapshot`. A provider implements
`owl_snapshot()` and returns the same `OntologyView`. Cross-process handoff uses
`encode_snapshot`, `decode_snapshot`, or `open_snapshot`; pickle and ontology
path round-trips are not part of the contract.

## What the package owns

- The complete immutable OWL 2 structural model and canonical identity.
- RDF/XML, Turtle, OWL/XML, and Functional Syntax acquisition and rendering.
- Explicit import resolution, provenance, limits, and secure offline defaults.
- Documents, resolved snapshots, persistent overlays, and zero-copy composites.
- Lazy structural indexes, fingerprints, and validated wire/cache transport.
- Equivalent complete Python behavior and optional private Rust acceleration.

It deliberately does not classify, realize, check consistency, project graph
edges, repair an ontology, or expose consumer-private IDs. See
[architecture and view lifecycles](docs/views-and-architecture.md).

## Release status

The distribution name is `pyowl-core`; the import package is `pyowl_core`.
The source tree carries the production `0.1.0` release. Native wheels are
optional optimizations and must never remove features from the pure fallback.
The 1.0 API remains a future compatibility milestone; it is not implied by the
production status of the 0.1 line.

## Documentation

- [Documentation index](docs/index.md)
- [API guide](docs/api.md)
- [Documents, snapshots, views, overlays, and composites](docs/views-and-architecture.md)
- [Parse-once consumer handoff](docs/consumer-handoff.md)
- [Security, imports, and caches](docs/security.md)
- [Native fallback troubleshooting](docs/troubleshooting.md)
- [Measured performance evidence](docs/performance.md)
- [Version and consumer compatibility](docs/compatibility.md)
- [Migration guide](MIGRATION.md)
- [Release checklist](docs/release-checklist.md)
- [Release, yank, and security rollback](docs/releasing.md)

Normative requirements begin at [the master specification](specs/SPEC.md).

## Compatibility boundary

- Distribution: `pyowl-core`
- Import: `pyowl_core`
- Python: `>=3.10`
- Current package line: production `0.1.x`
- API: `(0, 1)`; model schema: `1`; wire: `(1, 1)`; adapter protocol: `1`
- Runtime/build: no JDK, JRE, JVM, OWLAPI, JPype, ROBOT, Maven, Gradle, or Java archive
- Project source license: Apache License 2.0; packaged third-party notices are
  listed in `THIRD_PARTY_LICENSES/`

The exact tested consumer commits and ranges are recorded in
[`reports/integration/consumer-compatibility.json`](reports/integration/consumer-compatibility.json).
