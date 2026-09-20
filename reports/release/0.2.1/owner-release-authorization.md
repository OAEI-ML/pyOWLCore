# pyowl-core 0.2.1 release-owner decision

Date: 2026-09-20

The owner approved the scoped 0.2.1 benchmark and workflow changes and later
explicitly authorized completion of CI and publication through the trusted
workflows. The latest instruction supersedes the earlier preparation-only scope:

> Ok in any case I added the trusted publication configuration but i wont use
> testpypi lets overwrite that form the workflow, ensure all CI's are green and
> then publish using the trusted workflows that is probaly a better approach
> then the direct upload using the apitoken

The release uses the configured production PyPI Trusted Publisher:
owner OAEI-ML, repository pyOWLCore, workflow release.yml, environment pypi.
This configuration is owner-confirmed; the actual OIDC exchange and published
provenance must still be verified during the release. No long-lived API token
is used. TestPyPI is removed from this release workflow and its required gate
set; no TestPyPI execution or pass is claimed.

For 0.2.1 only, the previously approved bounded Linux optimization evidence and
installed-package integration described in benchmark-policy.md remain the
accepted performance-related evidence. The full legacy reference-host
comparator study was not executed or claimed by this decision.

The signed source tag, matching CI and Native safety results, full portable
artifact matrix, reproducibility, advisory scans, platform audits, exact source
and checksum verification, clean candidate installation, GitHub/Sigstore
attestations, and final production checks remain required. Signature gates close
only after their verifiers run. The production workflow publishes the identical
verified files and then checks PyPI hashes, PEP 740 provenance, and installation.

Historical decisions, measured source identities, and validation evidence remain
unchanged. This decision removes only the TestPyPI prerequisite and authorizes
trusted production publication; failed technical checks cannot be waived by it.
