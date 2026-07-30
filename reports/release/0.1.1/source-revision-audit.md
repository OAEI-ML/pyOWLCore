# Published 0.1.0 source-revision audit

Date: 2026-07-30

## Public files

PyPI exposes exactly two files for `pyowl-core==0.1.0`:

| File | Bytes | SHA-256 | Uploaded (UTC) |
| --- | ---: | --- | --- |
| `pyowl_core-0.1.0-py3-none-any.whl` | 435719 | `859c0a8d060fc8fb34b088966b8a54238d073a5fdff994a4fa7a910401069126` | `2026-07-30T01:37:09.622611Z` |
| `pyowl_core-0.1.0.tar.gz` | 2762669 | `98ecf1db443ea8103e94e3554b6b826260c3397d39037054fa2bd9e9012431cd` | `2026-07-30T01:37:16.823623Z` |

PyPI records neither file as yanked and exposes no provenance attestation for
either upload.

## Source comparison

The annotated `v0.1.0` tag object
`9fadd9f07a705b665334e7fb64a39706cc0b3745` resolves to commit
`d3e7893b0609fcd7df390375267a00356f09cb22`.

After independently downloading the public files and verifying the SHA-256
values above:

- every wheel member below `pyowl_core/` was byte-identical to
  `v0.1.0:src/pyowl_core/`;
- the sdist `src/pyowl_core/` tree was byte-identical to the tagged tree;
- the sdist `native/` tree was byte-identical to the tagged tree; and
- the sdist `pyproject.toml` and `pyowl_build.py` were byte-identical to the
  tagged files.

The corrective predecessor `4c3d4ac622c1a05a3cb6125d462e9925bd1d4c6b`
is 22 commits after `v0.1.0`. Across shipped implementation/build inputs,
eight files differ by 251 insertions and 49 deletions before the SemVer bump.
The final `0.1.1` tag subject must be the exact revision validated by Wheels,
native safety, and CI. The release owner inherited the existing paired DOID
performance decision because the patch does not change the measured public
contracts or semantics.

## Decision

Adding current native wheels to the existing `0.1.0` project version would mix
source revisions behind one immutable version. It would also conflict with the
already-published universal wheel and sdist names. The corrected source is
therefore released atomically as `0.1.1`; no `0.1.0` evidence is rewritten or
relabelled.
