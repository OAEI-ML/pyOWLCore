# Third-party license inventory

`inventory.toml` records every crate resolved by `native/Cargo.lock`. The
native extension has no Java/JVM dependency and the Python distribution has no
runtime Python dependency.

For dependencies offered under `MIT OR Apache-2.0`, release artifacts select
the Apache-2.0 option; its complete text is the repository-root `LICENSE` and
is included in every sdist and wheel. `target-lexicon` additionally carries the
LLVM exception in `LLVM-exception.txt`. `unicode-ident` requires the Unicode
License v3 text in `Unicode-3.0.txt` in addition to its selected Apache-2.0
terms.

The locked W3C RDF/XML test corpus under
`tests/data/corpus/w3c/rdfxml/rdf11` is development-only test data, not a
runtime dependency. It selects the W3C 3-clause BSD option recorded in
`W3C-RDF-tests-BSD-3-Clause.txt`; `UPSTREAM.toml` pins the exact repository
revision, upstream tree, file count, and aggregate digest.

This checked inventory is engineering evidence, not legal approval. A release
owner or counsel must approve the selected licenses and any source/NOTICE/
relinking obligations before publication. No Horned-OWL, LGPL, Java, OWLAPI,
ROBOT, JPype, DeepOnto, or mOWL component is linked or bundled by this package.
