# Phase 2B.2 passive live shadow operation

The shadow registry is isolated at `data/company_registry_shadow.db`. The live
scraper's CSV classifier remains authoritative. Shadow results never filter a
record, change a counter or timestamp, gate an export, or affect outreach.

## Initialize and inspect

Create the first consistent snapshot and migrate only that copy:

```bash
.venv/bin/python -m company_registry.shadow init \
  --production data/company_registry.db \
  --shadow data/company_registry_shadow.db
```

Initialization refuses to overwrite an existing shadow database. Inspect it:

```bash
.venv/bin/python -m company_registry.shadow inspect \
  --shadow data/company_registry_shadow.db
```

Refresh requires the explicit reset switch. The previous shadow database is
renamed to a timestamped backup before the replacement is installed:

```bash
.venv/bin/python -m company_registry.shadow refresh --reset \
  --production data/company_registry.db \
  --shadow data/company_registry_shadow.db
```

## Activate and deactivate

Maps shadow mode:

```bash
.venv/bin/python maps.py --incremental --company-registry-shadow \
  --known-companies-dir CSV_FILES \
  --shadow-database data/company_registry_shadow.db \
  --shadow-report-dir data/reports/company_registry_shadow
```

Google Search company shadow mode:

```bash
.venv/bin/python -m utils.google_search_discovery --incremental \
  --company-registry-shadow \
  --shadow-database data/company_registry_shadow.db \
  --shadow-report-dir data/reports/company_registry_shadow
```

Deactivate shadowing by omitting `--company-registry-shadow`. This is the
default. If shadow initialization or an observation fails, the primary pipeline
continues with its legacy decision.

Each enabled invocation creates a run-specific JSONL comparison log and summary
under `data/reports/company_registry_shadow/`. Persistent comparisons and run
associations are also stored in the shadow database. Stable source keys and
payload hashes prevent duplicate observations after a restart.
