# Troubleshooting

## Confirm the installed package

The distribution is named `pyowl-core`, while the import is `pyowl_core`:

```bash
python -m pip show pyowl-core
python -c "import pyowl_core; print(pyowl_core.__version__)"
```

If Python imports a checkout instead of the intended wheel, run the command
outside the repository and inspect `pyowl_core.__file__`.

## Native backend warning

`NativeBackendUnavailableWarning` under `backend="auto"` means the private
extension was absent, incompatible, or failed self-test. The complete Python
backend remains selected. To make behavior explicit, choose
`BackendPreference.PYTHON`; to require acceleration and fail closed, choose
`BackendPreference.NATIVE`.

Do not suppress the warning by installing Java or a second ontology parser.
Inspect wheel compatibility, Python/platform tags, extension self-test, and the
packaging evidence instead.

## Compiler-free installation

Use the pure `py3-none-any` wheel when available. A forced-pure sdist build sets
`PYOWL_CORE_BUILD_NATIVE=0`. It must not invoke Rust/C or Java compilers. If an
official pure artifact lacks any OWL feature, that is a release defect rather
than an optional-extra requirement.

## Existing view rejects options

Format, resolver, import, source-map, validation, backend, and root
`document_iri` choices apply during acquisition. They cannot be retroactively
applied to an existing view. Reuse the original compatible view or explicitly
load a new source at the application boundary.

## Imports remain unresolved

Check `snapshot.import_manifest`, the selected `ImportPolicy`, offline mode,
resolver attempts, and integrity diagnostics. Queries never fetch an import.
Strict policy fails rather than silently returning an incomplete closure.

## Wire/cache incompatibility

IPC raises a wire version/corruption error. A managed cache may discard a
supported incompatible entry and rebuild from trusted source. Never reinterpret
a new model schema or unknown required wire section as an older one.

## Performance expectations

Use the pinned evidence in [performance.md](performance.md). There is no
supported blanket “2x parsing” claim. Repeated parsing usually indicates a
consumer handoff bug; verify view identity and instrumentation counters before
tuning parser internals.
