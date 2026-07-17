# Coordinated vulnerability disclosure draft

## Private contact

Before a public release, replace the repository-access private channel named in
the root security policy with a monitored security email or private advisory
URL. Reports must include affected revision, impact, a minimal reproduction,
and whether public disclosure is already known. Do not request ontology data
that is unnecessary to reproduce the issue.

## Supported versions

The current unreleased `0.1.x` development line is the only supported line.
After publication, this section must list exact maintained minor versions and
end-of-support dates; unsupported branches receive no security claim.

## Response targets

- acknowledge a complete private report within three business days;
- provide an initial severity/reproduction assessment within seven days;
- agree on status updates and a disclosure window with the reporter; and
- publish a fixed version, advisory, affected-version range, and credit unless
  anonymity is requested.

These are targets, not promises that override emergency coordination.

## Coordinated disclosure

Maintain embargoed details only among people needed to reproduce, fix, audit,
and release. Assign a private tracking identifier, preserve the minimized
regression confidentially until release, request a CVE when appropriate, test
pure/native artifacts, and notify affected downstream consumers. If active
exploitation or public details change the risk, coordinate an accelerated
release with the reporter.

## Pre-1.0 release gate

Publication is blocked until the private contact is real and monitored, the
supported-version policy is explicit, the response owner accepts this process,
and package/repository metadata link to the final security policy. This draft
does not itself advertise a production security contact.
