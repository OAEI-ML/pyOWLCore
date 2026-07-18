# Release, verification, yank, and security rollback

This procedure is executable release machinery, not evidence that the
provisional `pyowl-core` name, repository URLs, or publishing identities are
already controlled.

## Roles and immutable inputs

The release owner selects a signed `v<version>` tag and records the exact
40-character commit. A second approver controls the protected `testpypi` and
`pypi` GitHub environments. Build jobs have read-only repository permissions;
only a publishing job receives `id-token: write`, and it receives no long-lived
index token.

The wheel workflow builds once from the selected commit and aggregates one
sdist, one `py3-none-any` pure wheel, and every approved native wheel. The
aggregate also contains SHA-256 checksums, pure/native SBOMs, archive inspection
results, platform dynamic-library audits, resolver results, advisory output,
and an incomplete release-decision report. Never rebuild between TestPyPI and
PyPI.

## Candidate sequence

1. Confirm project-name control, approved repository/docs/issues URLs, recovery
   contacts, trusted-publisher identities, and private security routing.
2. Freeze the API/model/wire/adapter versions, changelog, migration guidance,
   consumer ranges, dependency lock, and third-party inventory.
3. Run `wheels.yml` at the signed commit. Review every failed, blocked, and
   deferred field; absence of evidence is not a pass.
4. Download the aggregate by workflow run ID. Verify its run commit equals the
   signed tag, recompute every checksum, and generate the final report with
   `python -m tools.packaging.release_report ... --require-ready`.
5. Approve the `testpypi` environment. Upload the already-built files through
   Trusted Publishing, then install from TestPyPI into clean compiler-free and
   forced-native environments and rerun the consumer smoke matrix.
6. Approve the `pypi` environment only after the rehearsal evidence is attached
   to the same report. Upload the identical files; the official PyPA publishing
   action emits PEP 740 publish attestations by default.
7. Re-fetch index metadata, files, and provenance. Match all digests to the
   aggregate, install through the public resolver, verify the Trusted Publisher
   identity, then publish documentation and release notes.

PyPI attestations bind files to the publishing identity and digest; they do not
replace source review, tests, legal approval, or release-owner judgment. See
the [PyPI attestation security model](https://docs.pypi.org/attestations/security-model/).

## Incident decision

Stop publishing immediately, preserve logs and artifacts, and record the
affected version, hashes, platforms, impact, and reporter. Do not overwrite or
silently delete an immutable distribution.

| Condition | Required response |
| --- | --- |
| Upload still in progress | Cancel the publishing environment, revoke the workflow authorization, and determine exactly which files reached the index. |
| Corrupt, uninstallable, semantically divergent, or metadata/license-defective release | Yank the entire version with a specific reason; publish a corrected new version only after all gates rerun. |
| Security vulnerability | Use the private security route, assess exposure, prepare a coordinated fixed version/advisory, yank affected releases when warranted, and rotate/reconfigure any compromised publisher or account recovery path. |
| Wrong publishing identity or digest/provenance mismatch | Treat as a supply-chain incident, halt all releases, preserve evidence, revoke the publisher configuration, notify the index, and do not resume until ownership is re-established. |
| Secret accidentally included | Revoke the secret first; contact the index about deletion only when yank retention is itself unsafe, then assume every downloaded copy persists. |

[PyPI currently yanks whole releases, not individual files](https://docs.pypi.org/project-management/yanking/).
Therefore a defective native wheel cannot be removed while leaving the same
version's pure wheel normally selectable on PyPI: yank the version and publish
a new immutable version. Deletion is exceptional and never a substitute for a
yank, advisory, or versioned fix.

## Recovery closeout

After remediation, verify the fixed release from the public index, update the
security advisory/changelog/consumer constraints, document whether cached or
exact-pinned installs remain exposed, and perform a blameless review of the
failed gate. A release is closed only when the incident owner and release owner
sign off on the recorded hashes and actions.
