# pyowl-core 0.2.0 release-owner authorization

Date: 2026-08-02

The release owner explicitly authorizes production publication of
`pyowl-core 0.2.0` from the final validated source revision containing this
record. The authorization applies only after the exact-source Wheels workflow
builds and audits the complete 27-file candidate: one source distribution, one
universal wheel, and 25 native wheels. Only those checksum-bound files may be
published.

For this release, the owner explicitly:

- accepts the completed local schema-2 migrations for pyELK, pyHermiT,
  OWL2Vec* Projector, and OAEI Bio-ML; Exact-OM remains outside this
  coordinated publication scope;
- accepts the current Apache-2.0 dependency, NOTICE, third-party licence, and
  source-policy boundary, and waives `LIC-001` as-is. This is an owner risk
  decision, not legal advice and not a claim that counsel review occurred;
- confirms control of the existing production `pyowl-core` PyPI project and
  approves the OAEI-ML repository, documentation, and issue URLs;
- accepts the installed-native versus py-horned DOID common-contract result as
  sufficient performance evidence for `0.2.0`. The unrun full reference-host,
  extra-lane, FMA, and steady-RSS closeout remains disclosed and is not
  relabelled as executed evidence;
- authorizes creation of `v0.2.0` from the final validated source after its
  exact-source Wheels candidate passes;
- waives TestPyPI rehearsal and pre-upload signature requirements; and
- authorizes direct account-scoped PyPI API-token publication of the audited
  candidate, followed by public-index hash and clean-install verification.

The protected OIDC, TestPyPI, signature, and full native-performance workflows
remain available as a stronger optional promotion path. This authorization
does not claim that those optional jobs ran, that unavailable evidence exists,
or that a failed exact-source advisory or platform audit may be bypassed.
`advisory_scan` and `platform_artifact_audit` therefore remain staged in
`gates.json` until the Wheels aggregate replaces them with evidence from the
same source revision and immutable artifact set.
