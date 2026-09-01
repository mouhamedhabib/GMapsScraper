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

The builder normalizes unavailable values, rejects malformed or placeholder email addresses, merges records using supported company identifiers, prefers useful contact addresses, and flags conflicts instead of silently treating them as ready. `leads_ready.csv` is a quality-control output, not a guarantee that every address is deliverable or appropriate to contact.

The lead builder currently expects CSV columns produced by this scraper (`title`, `webpage`, `phone_number`, and `site_email`). Export with `-of CSV` before running it.

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
Maps scraper. Run it with:

```bash
python3 utils/google_search_discovery.py \
  -q google_queries.txt \
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
  -q google_queries.txt -l 15 --incremental --delay 3 --timeout 15
```

The normal `build_leads.py` command automatically reads this discovery file
when present. It merges Maps and Search records by the existing company
identity rules and records `source` and `source_queries` in `leads_master.csv`.
The four-column `leads_ready.csv` format remains unchanged.

Google Search discovery does not extract email addresses. Search-only records
therefore remain in `leads_master.csv` and do not enter `leads_ready.csv` until
a future, separate process obtains and validates an email address.

Ranked-list/article titles such as “163 Top startups in Tunisia for August
2026” are discovery noise, not company records. The current focused title/path
filter identifies this case; broader discovery cleanup is intentionally a
separate task.

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
