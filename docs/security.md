# Security, imports, plugins, and caches

## Secure acquisition defaults

`LoadOptions` defaults to offline operation and local import resolution.
`parse_document` never fetches imports. Network access requires a resolver that
explicitly allowlists schemes/hosts, redirect policy, byte limits, integrity
metadata, and credential handling. An ontology IRI is an identifier, not
automatic permission to fetch the same URL.

Caller-owned streams are read once and remain open. Text streams require an
explicit format and document IRI. A plain string is a filesystem path.

## Resource limits

Use `ParseLimits` and cancellation for untrusted inputs. Limits cover source
and closure bytes, documents/import depth, axioms/terms, nesting/RDF lists,
IRIs/literals, prefixes/diagnostics, redirects, memory, and deadlines. Wire
readers validate lengths/counts/checksums before allocation and reject unknown
required schema features.

## Plugins

Plugin discovery reads metadata only. It does not import plugin code until a
trusted explicit plugin name is selected. Installing a plugin must not change
auto-detection or resolver precedence. Parser plugins do not receive resolver
credentials unless the application explicitly authorizes them.

## Cache and IPC guidance

- Never unpickle ontology values.
- Authenticate the expected model/wire versions and fingerprints at trust
  boundaries; the decoder independently recomputes digests.
- Keep consumer compiler schemas and semantic options in consumer cache keys.
- Treat cache directories as data, not executable/plugin search paths.
- Use atomic write/durability policies for shared caches and do not weaken
  validation for mmap performance.

The release Java scan covers source, artifacts, nested archives, lockfiles,
SBOMs, build scripts, and download strings—not only `.jar` filenames.

