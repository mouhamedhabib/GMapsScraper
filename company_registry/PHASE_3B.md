# Phase 3B offline authoritative foundation

Phase 3B introduced the offline foundation. Phase 3C.1 now exposes it to the
Maps pipeline behind explicit opt-in modes; `legacy` remains the default and
Google Search is still not integrated.

## Database lifecycle

- `open_registry()` opens an existing registry only when it already has the
  exact supported schema. It never creates or migrates a database.
- `initialize_registry()` explicitly creates a new current-version registry.
- `migrate_registry()` explicitly migrates an existing registry.

Command-line initialization and migration are deliberately distinct:

```bash
python -m company_registry.storage init --database /tmp/registry.db
python -m company_registry.storage migrate --database /tmp/registry.db
```

Production migration requires its own approved backup procedure and is not part
of Phase 3B.

## Decisions and exports

`discovery_observations` retains the first stable source observation.
`discovery_run_decisions` stores the effective resolution for each run, so a
same-run retry is idempotent while a later run evaluates the observation again.

`AuthoritativeDiscoveryAdapter.preview()` is read-only and never authorizes an
export. `commit()` performs final transactional resolution and fails closed on
registry errors.

`export_new_companies()` rebuilds a deterministic CSV from committed
`NEW/CREATE_COMPANY` decisions. Its manifest is the publication commit marker;
an interrupted or failed export can be safely regenerated from SQLite.
