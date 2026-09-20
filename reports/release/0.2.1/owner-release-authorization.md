# pyowl-core 0.2.1 release-owner decision

Date: 2026-09-20

The owner explicitly approved the prepared 0.2.1 benchmark and workflow proposal:
"I aproved the proposed becnhmark/ workslow chnages please do that ill publish
to pypi separatly".

This authorizes committing and pushing the scoped benchmark policy and correcting
the release-check ordering. The owner will publish separately; the assistant
must not dispatch publication or upload distributions on the owner's behalf.
The earlier instruction to prepare and validate all four packages still applies.

For 0.2.1 only, the owner accepts the bounded Linux optimization evidence and
installed-package integration described in benchmark-policy.md instead of the
full legacy reference-host comparator study. This closes reference_performance
by an explicit scoped decision; it does not say that the full study ran.

The approved ordering correction permits only identified downstream checks to
remain pending until their verifiers execute. All applicable platform audits,
exact source/checksum checks, signed tags, TestPyPI, OIDC, attestations, and final
all-passed production checks remain required. The owner did not approve false
passes or publishing failed candidates. Actual implementation status is recorded
in the workflow and release handoff, separately from this authorization.

Historical 0.2.0 decisions and results remain unchanged. Other pending gates
must be closed using applicable evidence, not this authorization alone.
