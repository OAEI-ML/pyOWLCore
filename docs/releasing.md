# Release, verification, yank, and security rollback

This procedure is executable release machinery, not evidence that the
provisional `pyowl-core` name, repository URLs, or publishing identities are
already controlled.

## Roles and immutable inputs

The release owner selects a signed `v<version>` tag and records the exact
40-character commit. Publication runs in the `pypi` GitHub environment. Build jobs have read-only repository permissions;
only a publishing job receives `id-token: write`, and it receives no long-lived
index token.

The wheel workflow builds the candidate once from the selected commit and
independently rebuilds the sdist, pure wheel, and native wheels solely to prove
byte reproducibility; a rebuild is never substituted into the candidate. It
aggregates one sdist, one `py3-none-any` pure wheel, and every approved native
wheel. The aggregate also contains SHA-256 checksums, pure/native SBOMs,
archive inspection results, checksum-bound platform audits, the 29-target
resolver matrix, RustSec output, and an incomplete release-decision report.
Never rebuild between candidate verification and PyPI publication.

`SOURCE_DATE_EPOCH` normalizes wheel, tar, gzip, file-mode, ownership, and
macOS install-name metadata. Native compilation remaps checkout, Cargo registry,
Git checkout, and target-directory paths to stable virtual prefixes before the
binary-path audit. Reproducibility means identical bytes from the same pinned
source and toolchain; it does not authorize substituting a rebuild.

`reports/release/<version>/gates.json` is the reviewed input ledger. Keep a gate
`blocked` until its cited evidence exists; the report tool rejects unknown or
empty entries. The Wheels run replaces only the advisory and hosted
platform-audit entries after those checks pass. The Release run verifies and
records the signed-source entry. The attestation job installs directly from the
immutable candidate, runs the
native self-test and documented examples, and signs/verifies all 27 distributions
plus their checksums with GitHub/Sigstore provenance. It closes the signature gate
only after verifying those attestations, generates the release-ready report, and
signs/verifies that final report. PyPI publication consumes only that complete,
verified candidate. The owner removed the TestPyPI prerequisite; no TestPyPI
upload or rehearsal is performed or represented as a successful check.

## Version-scoped 0.2.1 benchmark decision

For 0.2.1 only, the owner approved bounded Linux native optimization validation
and installed-package integration as sufficient performance-related release
evidence. This does not claim that the full reference-host comparator study
ran or passed. Its methodology remains required before making the broader
comparative performance claims it supports.

`reports/release/0.2.1/performance-policy.json` binds the decision, scope, and
preserved validation files by SHA-256. Release may omit `performance_run_id`
only when `tools.packaging.release_performance` verifies that exact approved
version, every evidence digest, and the candidate's source/version identity.
The verified original evidence bytes accompany the candidate. Supplying a run
ID retains the existing full comparator verification; other versions require
that path. Release also requires successful CI and Native safety runs from the
same source commit. No package algorithm, measured result, artifact integrity
check, source-signature check, or publication control changes under this policy.

## Stage order and publisher configuration

A Wheels build may preserve pending source-signature, performance, attestation,
and publisher-configuration gates. Its candidate remains release_ready: false
until all required verifiers have run. Release requires the signed source and
accepted performance evidence before the attestation job. At that point only
signatures may remain pending. Failed gates or unexplained blockers stop the
stage. The production job requires every applicable gate passed and zero blockers.
Historical ledgers containing testpypi_rehearsal remain readable and that gate is
still enforced if supplied; the current production ledger does not require it.

The owner has confirmed the production Trusted Publisher for OAEI-ML/pyOWLCore,
workflow release.yml, environment pypi. This is configuration evidence, not an
upload receipt. The production job must still authenticate with OIDC and verify
the resulting public-index provenance. No account API token is used.

## Candidate sequence

1. Confirm project ownership, repository URLs, recovery contacts, the production
   Trusted Publisher identity, and the release-owner decision.
2. Freeze versions, migration guidance, dependency locks, and provenance records.
3. Build the complete Wheels candidate from the selected source. All applicable
   platform, reproducibility, advisory, resolver and artifact checks must pass.
4. Start release.yml from the matching signed version tag with the exact Wheels
   run ID. Supply performance_run_id for the full comparator path, mandatory
   outside the approved 0.2.1 evidence policy. The workflow verifies same-source
   CI and Native safety, the tag, run identities, every checksum and artifact.
5. Install directly from the candidate in a clean environment, run the native
   self-test and examples, and sign/verify the distribution set, checksum file,
   and final release-ready report. This step makes no index upload.
6. The pypi environment permits Trusted Publishing of those identical verified
   files. The PyPA action generates PEP 740 publish attestations.
7. Re-fetch index metadata, files, and provenance. Match all digests, verify all
   publish attestations against the repository, and install through the public
   resolver to verify native behavior.

PyPI attestations bind files to the publishing identity and digest; they do not
replace source review, tests, or release-owner judgment. See the
[PyPI attestation security model](https://docs.pypi.org/attestations/security-model/).

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
