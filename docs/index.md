# pyowl-core documentation

This documentation describes the implemented 0.1 development candidate and
the evidence required before 1.0. Normative behavior remains defined by the
files under `specs/`; examples never override those contracts.

- [API guide](api.md)
- [View and ownership architecture](views-and-architecture.md)
- [Consumer handoff](consumer-handoff.md)
- [Security](security.md)
- [Troubleshooting](troubleshooting.md)
- [Performance](performance.md)
- [Compatibility](compatibility.md)
- [Release checklist](release-checklist.md)

Executable examples:

- [`examples/parse_once.py`](examples/parse_once.py) demonstrates one in-process
  view shared across consumer boundaries and wire transport between processes.
- [`examples/secure_local_import.py`](examples/secure_local_import.py)
  demonstrates strict local mapping with network access disabled.

Both default to the complete Python implementation. Installed-artifact native
lanes run the same files with `PYOWL_CORE_DOCS_BACKEND=native`; forced native
fails rather than silently falling back when its wheel is unavailable or
incompatible.
