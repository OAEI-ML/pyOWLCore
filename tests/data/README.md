# WP09 conformance and hostile-input corpus

Every redistributed byte below is either locally authored from a cited
standards rule or preserved exactly from an explicitly licensed, revision-
locked upstream test suite. `PROVENANCE.toml` pins the applicable source,
revision/date, license terms, transformation statement, local SHA-256, and
review owner. `tools.corpus.manifest` fails closed if a corpus file is missing,
unregistered, mutable, or hash-mismatched.

The locally authored corpus remains deliberately compact and reviewable.
Generative and mutation suites derive bounded variants at test time; only
minimized regressions are retained. The official W3C RDF/XML suite is vendored
for full-manifest parity evidence and has an independent aggregate identity lock.
Large ontologies remain external hash-pinned manifests and are never downloaded
by ordinary tests.
