# Security and resource limits

## 1. Threat model

Ontology bytes, filenames, IRIs, import graphs, catalogs, HTTP responses, wire
caches, plugin metadata, and optional source maps may be attacker controlled.
The package must resist memory/CPU/disk exhaustion, parser differentials,
entity expansion, path escape, SSRF, decompression bombs, cache poisoning,
symlink races, malformed FFI/wire values, credential disclosure, and native
panic/undefined behavior.

Reasoning complexity is outside core, but core must not hand a consumer a value
advertised as complete/valid when parsing or imports were truncated.

## 2. Limit model

`ParseLimits` is immutable and propagated through parser, resolver, imports,
canonicalizer, indexes, wire, and writers. All components enforce the tighter
of caller/global constraints. Limits cover at least:

- source/document/closure bytes and document/import counts/depth;
- redirects, catalog rewrites, resolver attempts and concurrent fetches;
- triples, RDF lists, terms, strings, prefixes, IRIs, literals, annotations,
  axioms, expression/annotation nesting, rule atoms and sequence arity;
- diagnostics/source-map/origin entries;
- overlay depth/delta/composite members;
- index/wire rows/bytes, temporary files and disk cache;
- wall deadline/cancellation stride; and
- tracked heap/native/mmap/temp memory.

Defaults support large biomedical ontologies but are finite. Applications may
raise them explicitly. Limit errors include which named limit was exceeded and
observed/allowed counts without embedding hostile content.

No parser “recovers” by dropping excess items and returning a valid document.
No counter narrows silently. Count × size and offset + length are checked before
allocation/access.

## 3. XML and RDF hazards

All XML readers disable DTDs, external/general/parameter entity expansion,
XInclude, stylesheet execution, schema network retrieval, and custom object
hooks. Entity references outside predefined XML entities fail. Nesting/text
limits are applied during streaming, not after a DOM is built.

RDF collection traversal detects cycles/shared malformed tails and applies list
length/visited limits. Blank-node canonicalization uses algorithms with bounded
work and a limit for pathological symmetric graphs; exceeding it is explicit,
never parse-order fallback. Turtle lexer buffers token/string/comment size.

Unicode decoding is strict by format rules. Diagnostics bound excerpts and
escape control characters to prevent terminal/log injection.

## 4. Filesystem policy

Paths are opened under an optional allowlisted base using descriptor-relative/
platform-safe resolution where possible. Policies control symlinks, regular
files, device/FIFO/socket rejection, maximum size, ownership/permissions, and
cache roots. Validation and open are one race-resistant operation.

Import IRIs cannot be converted to paths by naive concatenation or percent
decoding. `DirectoryResolver` maps through a declared strategy then proves the
resolved target remains under its base. Catalog relative references resolve
against their catalog base under the same policy.

Cache/temp creation uses restrictive permissions and unpredictable exclusive
names. Atomic replace never follows an attacker-selected symlink. GC operates
only on validated content-addressed names below its root and does not recursively
follow links. Partial files are cleaned within quota.

## 5. Network/SSRF policy

Default `offline=True` means no DNS, socket, proxy, or implicit environment
network access. Network imports require an explicitly constructed `HttpResolver`
and `offline=False`, with:

- allowed schemes (HTTPS by default), exact hosts/ports and optional IP ranges;
- DNS results checked against private/link-local/loopback/metadata ranges before
  and after connection; redirect targets rechecked;
- maximum redirects, connect/read/overall deadlines and response bytes;
- streaming decompression with compressed/expanded limits and ratio cap;
- TLS verification and explicit proxy/environment policy;
- media type/format checks and optional mandatory SHA-256;
- bounded cache and revalidation; and
- credential isolation/redaction.

IRIs embedded in a document cannot override allowlists, add headers, select a
proxy/plugin, use `file:` under an HTTP resolver, or access cloud metadata.
Redirect/alias cycles raise typed resolution-cycle errors; ontology import graph
cycles remain legal.

## 6. Wire/cache trust

Wire validation order and allocation rules are normative in `wire-format.md`.
Even trusted cache files get structural bounds/reference validation. Digest
skipping never skips safety checks. Pickle/marshal/native memory dumps are
forbidden.

Content-addressed cache keys include schema/options and are verified against
content. A lock/sidecar is advisory, not authority. On corruption, only an
in-root recognized entry can be quarantined/removed; original source or an
authorized resolver is required to rebuild.

## 7. Plugins and supply chain

Entry points are not imported during ordinary discovery/auto parsing. Trusted
configuration selects an exact plugin name; policy may restrict distributions,
versions, hashes, and capabilities. Plugins receive only necessary data and the
same limits/cancellation. The core does not sandbox arbitrary Python plugins and
states that installing/selecting one grants code execution.

Release artifacts include locked build dependencies, hashes/provenance, SBOM,
license inventory, vulnerability audit, reproducible-build evidence where
practical, and scans for unexpected executables, shared libraries, network
install hooks, Java archives/classes, and bundled test secrets.

## 8. Native/FFI safety

The native policy in `native-backend.md` is mandatory: no borrowed lifetime
escape, checked scalars, panic containment, no abort, safe Rust by default,
bounded workers/allocations, signal/cancellation polling, and sanitizer/fuzz
coverage. Python validates native result framing before creating public values;
an extension version mismatch disables `AUTO` rather than guessing.

## 9. Denial-of-service methodology

Security benchmarks include adversarial, not just valid, scaling:

- exponential-looking nested expressions/annotations with depth limits;
- very long tokens, IRIs, literals, lists, rule argument vectors;
- RDF blank-node symmetry and collision-heavy dictionaries;
- huge duplicate sets and imports with diamonds/cycles/fan-out;
- slow/trickled/chunked/compressed HTTP responses;
- corrupt wire counts/offsets causing near-limit work;
- repeated small deltas/composites/index requests; and
- diagnostic floods.

For each, tests assert bounded memory/temp/duration to a configured limit,
prompt cancellation, deterministic error code, and no partially published
cache/snapshot.

## 10. Security release gates

- threat-model review and documented defaults on every new I/O feature;
- parser/wire native fuzzers run continuously with retained minimized corpus;
- Python property fuzz and malformed corpus across every backend/format;
- dependency/license/advisory/SBOM and Java-prohibition audits;
- SSRF/path/cache race integration tests on supported platforms;
- memory/allocation/deadline fault injection;
- no secret/credential/path leakage snapshots; and
- a published security contact, supported-version policy, and coordinated
  disclosure process before 1.0.

