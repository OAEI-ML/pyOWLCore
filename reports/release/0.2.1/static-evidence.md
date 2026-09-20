# 0.2.1 static release evidence

Date: 2026-09-20. This record closes only the applicable static checks. It is
not a portable-wheel result, benchmark waiver, signature, or upload receipt.

## Installed consumers

`consumer-stack.json` is the unchanged receipt from the isolated final four-wheel
integration run. All 75 Exact-OM ontology handoff, native projection,
consumption, hierarchy, and reasoner-adapter tests passed with zero skips or
failures. It binds the four installed 0.2.1 native wheel SHA256 values. The active
experiment environment was not modified. These are Linux test wheels; hosted
portable artifacts still require their own exact-source platform audits.

Receipt SHA256: `2ef8db2753b88b8c99e36989ebc7256668403c1aa5505036ca93ac630cb191d2`.
Original test-log SHA256: `50dd980327a5bf76b4c21d03535b2ab8635c1457d887409ce8f67756d878a57d`.
Both originals are retained in Exact-OM's
`data/experiments-v2/native-release-021/checks/`.

Tested package source revisions:

- Core: `adef8e54b35412b245223eb312a68dbd000ad01a`.
- Projector: `0359ac4cc043ddc9a34682381b8c7e27d33b626d`.
- ELK: `d96853f2c0920338822fab379d3fab07f611fa79`.
- HermiT: `bf270108a947fbbb65935e8670325bfdda5c0c11`.

Later release-only workflow or evidence edits do not constitute a new runtime
measurement. Runtime changes require renewed applicable validation.

## Project ownership and URLs

The owner explicitly confirmed: "im the owner of the pypi projects" and asked
to complete publication. This confirms project-name control; it does not prove
that any Trusted Publisher is configured. All configured core project URLs
returned HTTP 200 on this date and are unchanged from v0.2.0.

## Unchanged license and dependency boundary

An engineering comparison against the published v0.2.0 tag found byte-identical
LICENSE, NOTICE, and THIRD_PARTY_LICENSES files. The complete pyproject.toml,
native/Cargo.toml, and native/Cargo.lock differ only in this package's version.
There are no new third-party dependencies or license-policy changes. Inventory
SHA256: `3521d4761a32e184497841d1db24a52ddab91e2f59cbd7887822b27f3ab3d7f2`.

The current publication request covers releasing this unchanged policy boundary.
This is an engineering review of the release delta, not a new counsel review or
a newly asserted LIC-001 waiver. The historical issue and 0.2.0 owner decision
remain recorded unchanged. Advisory checks must still execute on the final
release source.
