# GMapsScraper — Google Maps Business Lead Scraper

GMapsScraper is a multi-threaded Python command-line tool that collects business data from Google Maps without requiring a Google Maps API key. It can also visit business websites to find email addresses and social links, export results as CSV, Excel, or JSON, and turn raw CSV output into deduplicated lead lists for review.

> This project is based on [GMapsScraper](https://github.com/Anonym0usWork1221/GMapsScraper) by Abdul Moez and has been modified and further developed to improve website contact extraction and turn raw Google Maps results into deduplicated, review-ready lead datasets.

## Features

- Scrapes multiple search queries concurrently with Selenium and `undetected-chromedriver`.
- Does not require a Google Maps API key.
- Supports headless and windowed Chrome sessions.
- Collects business details, contact data, images, and coordinates.
- Optionally checks business websites and common contact/about pages for email addresses and social profiles.
- Exports raw results to CSV, Excel (`.xlsx`), or JSON.
- Builds deduplicated CSV lead datasets, validates email candidates, and separates ready records from records that need review.

## Requirements

- Python 3.9 or newer
- Google Chrome Stable
- Windows, macOS, or Linux

The matching ChromeDriver is downloaded automatically. Internet access is therefore required when the driver is not already available locally.

## Installation

Clone or download this repository, then create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate the environment with:

```powershell
.venv\Scripts\activate
```

## Input

By default, the scraper reads `queries.txt`. Put one Google Maps search query or Google Maps URL on each line:

```text
software companies in Tunis Tunisia
web development companies in Tunisia
https://www.google.com/maps/search/IT+companies+in+Tunisia
```

The repository's current `queries.txt` contains Tunisia-focused examples; replace or edit them for your target market.

## Usage

Run the scraper with its defaults:

```bash
python maps.py
```

This reads `./queries.txt`, uses one worker, runs Chrome headlessly, and appends the results loaded during the one-minute default scroll window to `./CSV_FILES/google_maps_data.csv`.

A more complete example:

```bash
python maps.py \
  -q ./queries.txt \
  -w 2 \
  -l 100 \
  -bw 15 \
  -se contact \
  -se about \
  -o ./CSV_FILES \
  -of CSV \
  -sm 6
```

Use `python maps.py --help` to see the CLI help.

For incremental discovery, keep the CSV output in `CSV_FILES` and add
`--incremental`. Existing identities are loaded once from `leads_master.csv`,
`google_maps_data.csv`, and `google_search_companies.csv`. In this mode `-l`
counts newly accepted companies rather than already-known result cards:

```bash
python3 maps.py -q queries.txt -l 15 -o ./CSV_FILES -of CSV --incremental
```

### Command-line options

| Option | Description | Default |
| --- | --- | --- |
| `-q`, `--query-file` | Text file containing one query or Maps URL per line | `./queries.txt` |
| `-w`, `--threads` | Number of concurrent query workers | `1` |
| `-l`, `--limit` | Maximum results per query; `-1` means all available results | `-1` |
| `--incremental` | Skip known identities early and make `--limit` count new companies | off |
| `-u`, `--unavailable-text` | Text used when a value cannot be found | `Not Available` |
| `-bw`, `--browser-wait` | Browser and page wait timeout in seconds | `15` |
| `-se`, `--suggested-ext` | Website path to inspect for contacts; repeat for multiple paths | none |
| `-wb`, `--windowed-browser` | Show the Chrome window instead of running headlessly | off |
| `-nv`, `--disable-verbose` | Use the script's non-verbose status mode | off |
| `-o`, `--output-folder` | Folder used for the selected output file | `./CSV_FILES` |
| `-of`, `--output-format` | `CSV`, `EXCEL`, or `JSON` | `CSV` |
| `-sm`, `--scroll-minutes` | Maximum time spent loading additional results | `1` |

Website contact extraction is disabled unless at least one `-se` value is supplied. Values such as `contact`, `contacts`, `contact-us`, `about`, and `about-us` are expanded to common path variants; the original website URL is checked as well.

## Scraped fields

Raw output can include:

- Business name, category, rating, and price level
- Address, opening hours, phone number, website, and menu link
- Google Maps URL, cover image, related image URLs, latitude, and longitude
- Google Maps about/description data
- Website email address and Facebook, Instagram, Twitter, YouTube, and LinkedIn links when website extraction is enabled

Missing values use the text configured with `--unavailable-text`.

## Output

All queries write to one file in the selected output folder:

| Format | File name |
| --- | --- |
| CSV | `google_maps_data.csv` |
| Excel | `google_maps_data.xlsx` |
| JSON | `google_maps_data.json` |

Results are appended when the output file already exists. Rename or remove an old output file before a run if you want a fresh dataset.

## Build clean lead files

The lead builder accepts the scraper's CSV output and writes three derived files:

```bash
python utils/build_leads.py \
  --input ./CSV_FILES/google_maps_data.csv \
  --output-folder ./CSV_FILES
```

| File | Purpose |
| --- | --- |
| `leads_master.csv` | One consolidated company record with email status and review reasons |
| `leads_ready.csv` | Records that have an email and no detected review condition |
| `leads_review.csv` | Ambiguous or conflicting records requiring manual review |

The three lead files are staged and flushed before publication. Each pathname
is replaced atomically, and `leads_snapshot.json` is committed last to identify
the complete generation. Because a filesystem cannot atomically rename three
files together, a short commit window is recorded in a pending journal; a
handled failure rolls back immediately, while the next normal build recovers
the last complete snapshot after a process or power interruption. CSVs, transaction
metadata, backups, and directory entries are `fsync`-ed at this boundary where
the filesystem supports directory syncing.

The builder normalizes unavailable values, rejects malformed or placeholder email addresses, merges records using supported company identifiers, prefers useful contact addresses, and flags conflicts instead of silently treating them as ready. `leads_ready.csv` is a quality-control output, not a guarantee that every address is deliverable or appropriate to contact.

The lead builder currently expects CSV columns produced by this scraper (`title`, `webpage`, `phone_number`, and `site_email`). Export with `-of CSV` before running it.

### Enrich and finalize company context

Company enrichment keeps resumable progress in `leads_enriched.csv`. Publish
that working state through the structural finalizer before building outreach:

```bash
python3 -m utils.enrich_leads
python3 -m utils.finalize_enrichment
python3 -m utils.build_outreach
```

The finalizer validates row identities and CSV structure, fills columns missing
from older files with blank values, and atomically publishes the canonical
`CSV_FILES/leads_enriched_final.csv`. It does not change enrichment content or
statuses. `build_outreach.py` consumes that repository-owned final file by
default.

### Recover missing emails from any source

After building `leads_master.csv`, the missing-email enricher checks every lead
that lacks a valid email and has a usable HTTP(S) website. Maps, Search, and
mixed-source leads are all eligible. Progress is saved after every attempt in
`missing_email_enriched.csv`.

Test a 10-company batch:

```bash
python3 -m utils.enrich_missing_emails \
  --input CSV_FILES/leads_master.csv \
  --output CSV_FILES/missing_email_enriched.csv \
  --timeout 15 \
  --limit 10 \
  --verbose
```

For the full run, omit `--limit`. Then run `build_leads.py` again; it reads both
`search_email_enriched.csv` and `missing_email_enriched.csv` automatically.
Successful emails still pass through the builder's normal validation and
deduplication rules. Use `--retry-failed` to retry FAILED websites, up to the
three-attempt lifetime maximum.

If direct enrichment finishes as `NOT_FOUND` or `FAILED`, the bounded Search
fallback can inspect up to four company/domain-specific Google queries. It
opens at most two explicit contact/about results, shares one 10–15 second
deadline across the company, and checkpoints `search_email_fallback.csv` after
each company. For a controlled 20-company run:

```bash
python3 -m utils.search_email_fallback \
  --input CSV_FILES/leads_master.csv \
  --enrichment-input CSV_FILES/missing_email_enriched.csv \
  --output CSV_FILES/search_email_fallback.csv \
  --limit 20 \
  --timeout 12 \
  --windowed \
  --verbose
```

Use the windowed mode to solve Google verification manually if prompted; the
fallback does not automate CAPTCHA handling. The lead builder reads successful
fallback rows automatically and revalidates every email with its normal rules.

## Google Search company discovery

Google Search discovery is a separate, optional source and does not change the
Maps scraper. Since `google_queries.txt` now belongs to job discovery, pass a
company-oriented query file explicitly when using this legacy optional command:

```bash
python3 utils/google_search_discovery.py \
  -q queries.txt \
  -l 20 \
  --delay 3 \
  --timeout 15 \
  --verbose
```

Add `--windowed` to show Chrome. Discoveries are atomically written to
`CSV_FILES/google_search_companies.csv` with the columns
`company_name,website,source,source_query,source_url`. Repeated runs merge by
normalized website domain and preserve distinct search queries.

Add `--incremental` to skip domains already present in Maps, Search, or the
lead master and continue through current result pages until `-l` new domains
have been saved (or results/the deterministic inspection bound are exhausted):

```bash
python3 utils/google_search_discovery.py \
  -q queries.txt -l 15 --incremental --delay 3 --timeout 15
```

The normal `build_leads.py` command automatically reads this discovery file
when present. It merges Maps and Search records by the existing company
identity rules and records `source` and `source_queries` in `leads_master.csv`.
The four-column `leads_ready.csv` format remains unchanged.

Final grouping uses Maps place identity, website domain, exact name plus phone,
exact name plus company-email domain, then a conflict-free exact-name fallback.
An incoming row never unions multiple established groups: a unique strongest
compatible match may receive it, while tied or conflicting matches stay
separate and are routed to review. Distinct Maps places never merge through a
shared domain. Preview the result without writing any CSV with:

```bash
python3 utils/build_leads.py --analyze-identities \
  --input CSV_FILES/google_maps_data.csv --output-folder CSV_FILES
```

Google Search discovery does not extract email addresses. Search-only records
therefore remain in `leads_master.csv` and do not enter `leads_ready.csv` until
a future, separate process obtains and validates an email address.

Ranked-list/article titles such as “163 Top startups in Tunisia for August
2026” are discovery noise, not company records. The current focused title/path
filter identifies this case; broader discovery cleanup is intentionally a
separate task.

## Google Search job discovery (Phase 1)

The two discovery paths remain separate:

- `queries.txt` feeds `python maps.py` and discovers companies.
- `google_queries.txt` feeds `python -m job_search.discovery` and discovers
  individual job pages.

Job discovery recognizes Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Teamtailor, and conservative generic company career/job URLs.
It removes known tracking parameters, extracts provider job IDs where reliable,
fetches only the accepted page, and stores operational state in SQLite. It does
not perform candidate matching, contact discovery, email work, applications,
outreach, or any AI calls.

Run a small test that stops after three new jobs for each query:

```bash
.venv/bin/python -m job_search.discovery \
  --query-file google_queries.txt \
  --limit 3 \
  --delay 3 \
  --timeout 15 \
  --windowed \
  --verbose
```

For normal discovery:

```bash
.venv/bin/python -m job_search.discovery \
  --query-file google_queries.txt \
  --limit 20 \
  --delay 3 \
  --timeout 15 \
  --windowed \
  --verbose
```

`--limit` means maximum **new** jobs per query. Already-known jobs update
`last_seen_at` and normalized query provenance without consuming that limit;
rejected/noise results do not consume it either. Search pages use `start=0`,
`start=10`, `start=20`, and so on until the new-job target, exhaustion, a
repeated result page, verification, or the deterministic per-query inspection
cap `max(50, limit * 20)` is reached. In windowed mode, a Google verification
page pauses for manual completion; headless mode checkpoints SQLite and stops.

`google_queries.txt` is the authoritative job query file. Blank lines and lines
whose first non-whitespace character is `#` are ignored. Whitespace-normalized,
case-insensitive duplicates are executed once, while the first query's original
text is retained for Google and provenance. Queries are never derived from a CV
or rewritten. `queries.txt` remains the separate Maps/company input.

An optional recency hint can be sent to Google without changing the query text:

```bash
.venv/bin/python -m job_search.discovery --limit 3 --recent-days 14 --verbose
```

This adds Google's best-effort `tbs=qdr:d14` request parameter. It is not an
exact publication guarantee, and visible relative dates such as “2 days ago”
are retained only as search-result evidence. Only a job page/provider can set
`published_at`.

The default database is `data/job_search.db`. Tables are created automatically,
foreign keys are enabled on every connection, and `PRAGMA user_version` applies
the lightweight schema version. SQLite is authoritative; no CSV is used for job
state. `job_source_queries` preserves each query independently with first/last
seen timestamps and optional snippet, displayed-domain, and Google relative-date
evidence. Rediscovery through another query updates provenance without fetching
or duplicating a known job page.

## Daily Workflow v1

The daily workflow is a deterministic orchestration layer over the existing
Maps discovery, job discovery, completion, hard-filter, and review-priority
functions. It contains no AI, contact, outreach, application, or qualification
logic. The safe default does not run Google Maps: it discovers jobs, establishes
their scoped v1.1 decisions, completes only new REVIEW jobs (bounded by
`completion_limit`), re-filters only this run's new jobs, computes their review
priorities, and writes a report.

Settings live in `config/daily_workflow.json`; command-line options override
them. Each operational run creates `workflow_runs` and attaches every observed
job to `workflow_run_jobs` as `NEW`, `KNOWN`, or `UPDATED`. Reports therefore
use run membership rather than a calendar date. JSON is atomically published to
`data/reports/run_<run_id>.json` and, for SUCCESS or PARTIAL runs,
`data/reports/latest.json`. The default shortlist contains new PASS jobs and new
HIGH-priority REVIEW jobs; add `--include-medium` explicitly to include MEDIUM.

Validate the daily setup without opening Chrome or changing SQLite:

```bash
.venv/bin/python -m job_search.daily_workflow --dry-run --jobs-only
```

Run a small jobs-only daily test:

```bash
.venv/bin/python -m job_search.daily_workflow --jobs-only --job-limit 1 --completion-limit 3 --recent-days 14 --delay 3 --timeout 15 --windowed
```

Run the normal daily workflow with incremental Maps discovery enabled:

```bash
.venv/bin/python -m job_search.daily_workflow --with-maps
```

Display the latest report without web requests, browser startup, or job writes:

```bash
.venv/bin/python -m job_search.daily_workflow --report-only
```

Use `--report-only --run-id <run_id>` for a specific run. A failed optional
phase is recorded and the workflow continues safely as PARTIAL; individual
completion failures are already isolated by the completion module. Ctrl+C marks
the persistent run INTERRUPTED and does not replace the last good `latest.json`.

Filter newly stored jobs with deterministic policy `v1.1` (no network or AI calls):

```bash
.venv/bin/python -m job_search.filtering
```

Use `--rebuild` to replace evaluations for the selected policy version, or keep
historical decisions under a new version with `--policy-version v2`. Policy v1.1
uses only `published_at` for age decisions and records deterministic source quality
(`DIRECT_COMPANY`, `ATS`, `JOB_PLATFORM`, or `UNKNOWN`). The shortlist
views are read-only:

```bash
.venv/bin/python -m job_search.filtering --show-pass
.venv/bin/python -m job_search.filtering --show-review
```

Generic collection pages remain in SQLite for audit/history, but discovery now
rejects them with `GENERIC_JOB_LISTING_PAGE`; rebuilding filter policy `v1.1` marks
any already-stored collection rows as rejected without deleting or changing job
identity. To refill missing fields on existing generic individual postings, make
one bounded HTTP request per incomplete row with:

```bash
.venv/bin/python -m job_search.maintenance --limit 23 --verbose
```

If an individual site rejects normal HTTP (for example with a Cloudflare 403), an
explicit fallback can reuse one browser while still visiting only each selected
job URL, without crawling links:

```bash
.venv/bin/python -m job_search.maintenance \
  --job-id 14 --timeout 60 --browser-fallback --windowed --verbose
.venv/bin/python -m job_search.filtering --policy-version v1.1 --rebuild
```

For a deterministic repair of explicit `REVIEW` rows, use the review repair
command. It fills only blank fields, re-filters each successfully repaired row,
and records each attempt. `--final-pass` requires explicit job IDs and marks
them ineligible for later automatic repair retries:

```bash
.venv/bin/python -m job_search.repair_reviews \
  --job-id 1 --job-id 13 --job-id 20 \
  --final-pass --verbose
```

### Query performance analytics

Query analytics are read-only and use exact workflow-run membership and
run-scoped query provenance:

```bash
.venv/bin/python -m job_search.query_performance \
  --database data/job_search.db --recent-runs 10
.venv/bin/python -m job_search.query_performance \
  --database data/job_search.db --run-id RUN_ID --show-review
```

The default window is the 10 most recent `SUCCESS` or `PARTIAL` runs. `DROP`
requires at least 2 runs and 20 inspected results, zero new jobs, plus at least
80% rejected noise or 50% resolution failures. `KEEP` requires at least 2 runs,
10 inspected results, 2 new jobs, 5% candidate yield, a shortlisted downstream
outcome, and noise/failure rates below the drop limits. Everything ambiguous or
undersampled is `REVIEW`. Historical metrics not persisted by older schema
versions print as `unavailable`, never as an invented zero.

### Job qualification

Qualification is a deterministic, persisted verification layer for PASS and
HIGH-priority REVIEW jobs. MEDIUM reviews require explicit inclusion; hard-filter
REJECT jobs are never selected. It verifies posting identity/activity, employer
evidence, normalized location evidence, source relationship, and the strongest
safe application destination without using CV/profile data:

```bash
.venv/bin/python -m job_search.qualification \
  --database data/job_search.db --policy-version v1.1 --run-id RUN_ID

.venv/bin/python -m job_search.qualification \
  --database data/job_search.db --policy-version v1.1 \
  --job-id 326 --include-medium --timeout 15 --verbose
```

Qualification reads stored evidence first and makes at most one bounded HTTP
verification request when activity or the application channel remains unknown.
Browser fallback is disabled unless `--browser-fallback` is supplied. HTTP
404/410 or an explicit closed/removed state is inactive; 403, CAPTCHA,
Cloudflare, timeout, and temporary network failures remain unknown.

Source relationships can then be resolved from stored ATS tenant evidence with
no network requests. The resolver requires normalized exact agreement between
the Greenhouse, Lever, or Ashby tenant and the stored company, preserves known
DIRECT/RECRUITER/AGGREGATOR relationships, and records mutations in the existing
repair provenance:

```bash
.venv/bin/python -m job_search.relationship_resolution \
  --database data/job_search.db \
  --job-id 81 --job-id 142 --job-id 245 --job-id 248 --verbose
```

### Docker

The job command can run in Docker with:

```bash
docker compose run --rm job-search \
  python -m job_search.discovery \
  --limit 20 --delay 3 --timeout 15 --verbose
```

Compose stores `/app/data/job_search.db` in the named volume
`job_search_data`, which stays in Docker/WSL Linux storage instead of binding a
database file to a Windows NTFS path. The default container run is headless;
use the local windowed command when manual Google verification is needed.

## Troubleshooting

- Confirm Google Chrome Stable is installed and can be launched by the current user.
- Confirm the query file exists and contains at least one non-empty line.
- Increase `--browser-wait` for slow Maps or business website pages.
- Increase `--scroll-minutes` when large result sets stop loading too early.
- If the browser window closes or Google changes the Maps page structure, rerun with `-wb` to observe the page and diagnose selector or consent-screen issues.
- Do not keep the output workbook or CSV open in an application that locks it while the scraper is writing.

Google Maps and third-party websites can change their markup or restrict automated access, so scraping behavior may require maintenance over time. Use this software responsibly and comply with applicable laws, website terms, privacy requirements, and outreach rules.

## Credits and license

The original [GMapsScraper](https://github.com/Anonym0usWork1221/GMapsScraper) was created by Abdul Moez and is distributed under the MIT License. This modified version retains the original copyright notice and records the copyright for its modifications in [LICENSE](LICENSE):

```text
Copyright (c) 2025 Abdul Moez
Modifications and additional features:
Copyright (c) Chaieb Mohamed habib
```

See [LICENSE](LICENSE) for the full MIT License text.
