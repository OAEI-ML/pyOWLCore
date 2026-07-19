# Schema tag discipline

Each future model or wire ledger is a restricted TOML document with `schema`,
`namespace`, and `[[tag]]` entries containing a symbolic `name`, positive
integer `value`, and `active` or `retired` status. Tags are permanent: delete by
retiring, never by removing or reusing the number.

```console
python -m tools.schema.tags check schemas/model-v1.toml --previous old.toml
python -m tools.schema.tags generate schemas/model-v1.toml generated.py --check
python -m tools.schema.native_snapshot_publication_v2 check
python -m tools.schema.native_snapshot_publication_v2 generate
```

The tag generator sorts tags by numeric value and writes its generated Python
module atomically. The native-publication renderer preserves the executable
semantic-tree order and replaces its TOML ledger atomically; its `check`
command and publication-handoff drift test require byte-for-byte agreement.
WP01 owns the first model ledger; WP00 intentionally creates no ontology
constructor tags. WP15 owns only the V2 native-publication renderer as an
explicit handoff amendment; WP17 retains ownership of the remaining schema
tooling.
