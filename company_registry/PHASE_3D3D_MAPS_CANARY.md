# Phase 3D.3D Replacement Maps Qualification Canary

Status: **PASS**. Exactly one authorized Google Maps process ran for durable
run `phase3d3d-maps-20261009T190211Z`; no retry or Google Search process ran.

## Safety and execution

Preflight verified schema v5, `integrity_check=ok`, zero foreign-key
violations, exact historical company/branch/provenance preservation, the prior
run still `PARTIAL`, the documented production/shadow hashes, all 65 protected
CSV/XLSX hashes, Git-ignored outputs, and a writable process-scoped HOME. The
one-worker authoritative canary used the approved Tunisia software-development
query, disposable registry, limit 3, and isolated runtime/export paths. It
exited 0 and finalized `SUCCESS` by `GLOBAL_LIMIT_REACHED`.

## Discovery

- Cards inspected: 51; NEW 3, KNOWN 48, UPDATED 0, AMBIGUOUS 0,
  QUARANTINED 0; same-run duplicates 0.
- Created: 3 companies and 3 associated branches. The separate CREATE_BRANCH
  counter was 0.
- Budget: committed 3, reserved 0, remaining 0 of 3.
- Resolver/registry errors: 0. Duplicate run decisions: 0. Duplicate Place
  IDs: 0.

## Qualification of new companies

| Company | Website | Employment | Mission | Evidence and reasons |
| --- | --- | --- | --- | --- |
| `5dfd75d0-72e1-582c-a17d-1593c9cdcf30` — xTECH | `http://xtech.guru/` usable | TARGET / ELIGIBLE | POSSIBLE / REVIEW | Maps category `Software company`; strong software/IT evidence plus usable website and non-conflicting location. Mission service fit is unproven. |
| `36ccfe2c-0b58-5709-9ed3-7ccb5aa67c69` — FullStack | `http://www.fullstack.tn/` usable | NOISE / EXCLUDED | POSSIBLE / REVIEW | Maps category `Computer consultant`; policy recorded no qualifying software/IT business evidence. Commercial business, but mission service fit is unproven. |
| `2b62a69d-3d62-563c-9833-d36ceedbcebb` — Simple Concept | `https://www.simple.tn/` usable | NOISE / EXCLUDED | POSSIBLE / REVIEW | Maps category `Website designer`; policy recorded no qualifying software/IT business evidence. Commercial business, but mission service fit is unproven. |

All three had valid distinct Place IDs, identity/location conflict flags false,
and empty conflict lists. The evidence does not establish hiring status or a
confirmed software-service need.

## Exports

All manifests independently verified:

- `new_companies_phase3d3d-maps-20261009T190211Z.csv`: 3 rows, SHA-256
  `1c66a47c91c5b88e4b49ad15cd14d0627c3076f6f664c07ea7a432a55f116a45`;
  company IDs: `5dfd75d0-72e1-582c-a17d-1593c9cdcf30`,
  `2b62a69d-3d62-563c-9833-d36ceedbcebb`, and
  `36ccfe2c-0b58-5709-9ed3-7ccb5aa67c69`.
- `qualified_employment_leads_phase3d3d-maps-20261009T190211Z.csv`: 1 row,
  SHA-256 `04a94e3bdcb161a5192194a5673a9ef2f9c10be33b925d8ffb75607c3c07e0ed`;
  company ID: `5dfd75d0-72e1-582c-a17d-1593c9cdcf30` only.
- `qualified_mission_leads_phase3d3d-maps-20261009T190211Z.csv`: 0 rows,
  SHA-256 `1cf6088448911ab0578a45c7711b9880c5b9f5f058c4e7114b43282f85d2114b`;
  company IDs: none.

No REVIEW or EXCLUDED assessment entered an ELIGIBLE-only export.

## Final integrity

SQLite integrity is `ok` with zero foreign-key violations. Historical source
records and protected historical company/branch fields have zero differences
from production. Production remains
`ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`;
shadow remains
`ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
All 65 protected files match, including `leads_master` and every outreach file.
No application code, production/shadow data, outreach, or Phase 3E work changed.
