# Approved scoped performance policy for 0.2.1

The release owner approved this version-specific policy on 2026-09-20; see
owner-release-authorization.md. The checked performance-policy.json binds this
record, that approval, and the unmodified evidence files by SHA256.

For 0.2.1, the completed Linux native optimization validation and installed
four-package integration are sufficient performance-related release evidence.
The full Native performance comparator study on the former macOS implementation
machine is not a prerequisite for this release. It remains available, and its
methodology is still required for the broader claims it was designed to assess.
Future versions cannot inherit this exception automatically.

## Accepted evidence

The September 14 NCIT/DOID validation passed structural-baseline comparisons
and 128 independently reconstructed ordered feature rows (64 per ontology).
The recorded single preprocessing observations were 3999.53 to 495.44 seconds
for NCIT and 184.28 to 40.60 seconds for DOID. Candidate projection observations
were 177.86 and 15.48 seconds respectively. The NCIT candidate observation was
reused from its preserved successful attempt, not measured again during recovery.

Historical tested commits (development version labels were still 0.2.0):

- Core: 5fd93c8839318f0ccc1afff4d3ec537509c72e87.
- Projector: a0676012c5f6437c304b4ed04bc2b41b6a083da1.
- ELK: bce95f8552ed8396315c9a9e4ddce184ad95596f.
- HermiT: bbf31c26ce7ae03bd092f04bcb8955466b1fe3e0.

The copied native-optimization/ artifacts preserve the original identities and
results. Both feature implementations used the documented deterministic-order
correction; older nondeterministic feature digests did not match and are not
relabeled as matching. The original interpretation remains in Exact-OM's
specs/native-optimization/IMPLEMENTATION.md.

Separately, September 20's final installed 0.2.1 package stack passed 75 tests
with no failures or skips. consumer-stack.json binds all four actual native
wheel hashes; consumer-stack-tests.log preserves the test result. Tested source
identities and the unchanged dependency-policy review are in static-evidence.md.

## Limits and retained checks

These are bounded observations and correctness checks, not a completed full
comparator study, repeated confidence/RSS gates, final portable-wheel benchmarks,
or evidence of FMA/SNOMED, general reasoner, macOS, or Windows performance.
Later release-only edits do not change the historical measured source identities.

The exception changes only the performance-study prerequisite. It does not waive
package CI, Native safety, advisory scans, the full portable artifact matrix,
exact checksums/source identity, signed tags, OIDC, attestations, or public-index
verification. The later publication decision in owner-release-authorization.md
separately removes TestPyPI and authorizes the trusted production workflow.
Actual technical checks must pass before production publication.
