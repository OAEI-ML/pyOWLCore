# pyowl-core documentation

This documentation describes the implemented 0.1 development candidate and
the evidence required before 1.0. Normative behavior remains defined by the
files under `specs/`; examples never override those contracts.

## Install

```bash
python -m pip install pyowl-core
```

The distribution is `pyowl-core`; Python code imports `pyowl_core`. The
portable implementation is complete and compiler-free. Native wheels are
optional accelerators with the same public values and semantics.

## Start here

- New users: run [`examples/parse_once.py`](examples/parse_once.py), then read
  the [API guide](api.md).
- Applications sharing ontologies across components: read
  [consumer handoff](consumer-handoff.md).
- Applications resolving imports or handling untrusted inputs: read
  [security](security.md).
- Deployments requiring native acceleration: read
  [troubleshooting](troubleshooting.md) and [performance](performance.md).

## Reference

- [API guide](api.md)
- [View and ownership architecture](views-and-architecture.md)
- [Consumer handoff](consumer-handoff.md)
- [Security](security.md)
- [Troubleshooting](troubleshooting.md)
- [Performance](performance.md)
- [Compatibility](compatibility.md)
- [Specification traceability](spec-traceability.md)
- [Release checklist](release-checklist.md)
- [Release, yank, and security rollback](releasing.md)

Executable examples:

- [`examples/parse_once.py`](examples/parse_once.py) demonstrates one in-process
  view shared across consumer boundaries and wire transport between processes.
- [`examples/secure_local_import.py`](examples/secure_local_import.py)
  demonstrates strict local mapping with network access disabled.

Both default to the complete Python implementation. Installed-artifact native
lanes run the same files with `PYOWL_CORE_DOCS_BACKEND=native`; forced native
fails rather than silently falling back when its wheel is unavailable or
incompatible.
