# Security policy

Do not open a public issue for a suspected vulnerability. Until a private
security contact is published with the eventual repository URL, contact the
maintainers through the private channel used to grant repository access and
include affected revision, impact, reproduction, and any proposed mitigation.
This temporary routing must be replaced before public release.

Supported security fixes currently target the unreleased `0.1.x` development
line. No release is claimed secure or production-ready yet.

Ontology documents, IRIs, imports, caches, source maps, plugin metadata, and
wire input are untrusted. Contributions must apply `ParseLimits`, preserve the
offline/local default, reject partial-success truncation, avoid hostile content
in diagnostics, and follow `specs/security.md`. Never fetch a corpus or invoke
Java during ordinary tests, import, build, or installation.
