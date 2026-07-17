# Schema tag discipline

Each future model or wire ledger is a restricted TOML document with `schema`,
`namespace`, and `[[tag]]` entries containing a symbolic `name`, positive
integer `value`, and `active` or `retired` status. Tags are permanent: delete by
retiring, never by removing or reusing the number.

```console
python -m tools.schema.tags check schemas/model-v1.toml --previous old.toml
python -m tools.schema.tags generate schemas/model-v1.toml generated.py --check
```

The generator sorts by numeric value and writes atomically. WP01 owns the first
model ledger; WP00 intentionally creates no ontology constructor tags.
