# WP09 — conformance, differential, fuzz, and security evidence

## Goal

Complete the standards/provenance/deviation corpus and cross-cutting evidence
that Python/native parsers, imports, views, and wire are safe and conformant.

## Read first

`verification.md`, `security.md`, `references.md`, all focused implemented specs,
and WP02/WP03/WP04/WP06/WP08 handoffs.

## Depends on

WP02, WP03, WP04, WP06 and WP08.

## Owned paths

Conformance/security/fuzz/data/corpus/report tools in manifest. Fixes to another
WP's code are coordinated with that owner, not patched around in the harness.

## Deliverables

- Provenance-licensed pinned W3C/format/errata/cross-syntax/hostile corpora and
  generated constructor coverage ledger.
- Full Python/native/independent/external differential harness and deviations
  registry; optional Java oracles isolated and never part of package/runtime.
- Generative/metamorphic suites for model/doc/import/overlay/composite/wire.
- Parser/wire native fuzzers, Python fuzz, sanitizer/fault/resource/path/SSRF/
  cache-race matrices and minimized regression workflow.
- Release-ready conformance/security evidence reports and supported security
  process/contact draft.

## Acceptance

- no unexplained/ownerless deviations or broad xfails/skips;
- every W3C constructor/production/tag/visitor/parser/writer/wire branch covered;
- ordinary import cycles and canonical language/blank identity edge cases pass;
- hostile inputs stay within configured bounds with no panic/leak/partial cache;
- external disagreement resolved against primary standard; and
- evidence reproducible from hashes without requiring Java in normal lanes.

