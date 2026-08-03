# Security policy

Do not open a public issue for a suspected vulnerability. Until a private
security contact is published for the repository, contact the maintainers
through the private channel used to grant repository access and include
affected revision, impact, reproduction, and any proposed mitigation.

Supported security fixes currently target the published `0.1.x` line and the
`0.2.0` release candidate. A defective release is yanked and replaced by a new
immutable version rather than modified in place; the full incident procedure
is in [docs/releasing.md](docs/releasing.md).

Ontology documents, IRIs, imports, caches, source maps, plugin metadata, and
wire input are untrusted. Contributions must apply `ParseLimits`, preserve the
offline/local default, reject partial-success truncation, avoid hostile content
in diagnostics, and follow `specs/security.md`. Never fetch a corpus or invoke
Java during ordinary tests, import, build, or installation.
