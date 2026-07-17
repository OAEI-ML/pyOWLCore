# Foundation audits

Run all offline policy checks with:

```console
python -m tools.audit.check_all
```

The checks validate the architecture boundary, curated public exports, frozen
metadata/version constants, LICENSE/NOTICE and fixture provenance, and the
absence of Java artifacts or Java-backed runtime/build dependencies. Artifact
scanning is repeated against built archives during release work; this source
audit is the WP00 guardrail.
