"""
Search-Only Email Enrichment
============================

Purpose:
    Find public website emails for companies discovered only through Search.

Pipeline:
    leads_master.csv -> enrich_search_emails.py -> search_email_enriched.csv
                                                   |
                                                   +-> build_leads.py

Input:
    Master lead rows, especially Search-only records with a website but no email.

Output:
    A resumable enrichment CSV with selected emails, geographic metadata,
    status, and attempt count.

Previous / next:
    ``build_leads.py`` creates the input and normally runs again after this file.
"""

from argparse import ArgumentParser
from csv import DictReader, DictWriter
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit, urlunsplit

if __package__:
    from utils.build_leads import (
        MASTER_FIELDS,
        clean_value,
        email_sort_key,
        email_status,
        extract_emails,
        is_valid_email,
        website_domain,
    )
    from utils.enrich_leads import BrowserSession, create_chrome_driver, is_driver_failure
else:  # Support direct execution from the utils directory.
    from build_leads import (
        MASTER_FIELDS,
        clean_value,
        email_sort_key,
        email_status,
        extract_emails,
        is_valid_email,
        website_domain,
    )
    from enrich_leads import BrowserSession, create_chrome_driver, is_driver_failure


DEFAULT_INPUT = Path("./CSV_FILES/leads_master.csv")
DEFAULT_OUTPUT = Path("./CSV_FILES/search_email_enriched.csv")
# ---------------------------------------------------------------------------
# RESUMABLE EMAIL-LOOKUP POLICY
# ---------------------------------------------------------------------------
# Persist every attempt and cap lifetime retries so chronically unavailable
# websites do not consume the browser on every future run.

MAX_LIFETIME_ATTEMPTS = 3
EMAIL_PAGE_PATHS = (
    "contact",
    "contact-us",
    "contacts",
    "about",
    "about-us",
)
OUTPUT_FIELDS = (*MASTER_FIELDS, "email_enrichment_status", "email_enrichment_attempts")


class WebsiteCheckFailed(RuntimeError):
    """Signal that no candidate page supplied usable HTML for inspection."""

    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind


def read_csv(path):
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def atomic_write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8-sig",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file_handler:
            temporary_path = Path(file_handler.name)
            writer = DictWriter(file_handler, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def parse_attempts(value):
    try:
        return max(0, int(clean_value(value) or "0"))
    except ValueError:
        return 0


def source_values(row):
    return {
        value.strip().casefold()
        for value in clean_value(row.get("source")).split(";")
        if value.strip()
    }


def is_search_only(row):
    sources = source_values(row)
    return "google_search" in sources and "google_maps" not in sources


def valid_row_emails(row):
    values = []
    seen = set()
    for field in ("email", "alternate_emails"):
        for email in extract_emails(row.get(field)):
            key = email.casefold()
            if is_valid_email(email) and key not in seen:
                seen.add(key)
                values.append(email)
    return values


def row_identity(row, row_index):
    domain = website_domain(row.get("website"))
    if domain:
        return "domain", domain
    name = clean_value(row.get("name")).casefold()
    return ("name", name) if name else ("row", str(row_index))


def existing_index(rows):
    return {
        row_identity(row, index): row
        for index, row in enumerate(rows)
    }


def prepare_records(
    input_path,
    output_path,
    target_predicate=is_search_only,
    website_predicate=None,
):
    """Merge current master rows with prior enrichment progress and statuses."""
    if website_predicate is None:
        website_predicate = lambda row: bool(website_domain(row.get("website")))
    current_rows = read_csv(input_path)
    previous = existing_index(read_csv(output_path))
    records = []
    seen = set()

    for row_index, input_row in enumerate(current_rows):
        identity = row_identity(input_row, row_index)
        if identity in seen:
            continue
        seen.add(identity)
        prior = previous.get(identity, {})
        # Copy the full master contract so email lookup cannot discard source
        # geography; old CSVs simply supply blanks for the missing keys.
        record = {field: clean_value(input_row.get(field)) for field in MASTER_FIELDS}

        prior_emails = valid_row_emails(prior)
        input_emails = valid_row_emails(input_row)
        prior_status = clean_value(prior.get("email_enrichment_status")).upper()
        prior_found = prior_status == "FOUND" and bool(prior_emails)
        if prior_found:
            set_record_emails(record, prior_emails)
        elif input_emails:
            set_record_emails(record, input_emails)
        else:
            record["email"] = ""
            record["alternate_emails"] = ""

        attempts = max(
            parse_attempts(prior.get("email_enrichment_attempts")),
            parse_attempts(input_row.get("email_enrichment_attempts")),
        )
        if target_predicate(record):
            if valid_row_emails(record):
                status = "FOUND"
            elif not website_predicate(record):
                status = "NO_WEBSITE"
            elif prior_status in {"PENDING", "NOT_FOUND", "FAILED"}:
                status = prior_status
            else:
                status = "PENDING"
        else:
            status = "FOUND" if prior_found else ""
            attempts = attempts if prior_found else 0
        record["email_enrichment_status"] = status
        record["email_enrichment_attempts"] = str(attempts)
        records.append(record)
    return records


def candidate_urls(website, url_creator):
    """Return a deduplicated homepage/contact/about inspection order."""
    candidates = url_creator(website, list(EMAIL_PAGE_PATHS))
    parsed = urlsplit(website if "://" in website else "http://" + website)
    root = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "/", "", ""))
    ordered = candidates[:1] + [root] + candidates[1:]
    unique = []
    seen = set()
    for url in ordered:
        key = url.rstrip("/") or url
        if key not in seen:
            seen.add(key)
            unique.append(url)
    return unique


def extract_public_emails(scraper, driver, website):
    """Return validated public emails found on reachable candidate pages."""
    sources = scraper.get_source_code(
        driver,
        candidate_urls(website, scraper.create_urls),
    )
    failure_kind = getattr(scraper, "last_failure_kind", None)
    failure_message = getattr(scraper, "last_failure_message", "")
    if not sources:
        raise WebsiteCheckFailed(
            failure_message or "no candidate website page could be loaded",
            kind=failure_kind or "other",
        )
    emails = scraper.get_pattern_data(sources).get("site_email", [])
    valid_emails = [email for email in emails if is_valid_email(email)]
    if not valid_emails and failure_kind in {"dns", "timeout", "connection"}:
        # A partial page set with a terminal network failure is inconclusive,
        # not proof that the company publishes no email.
        raise WebsiteCheckFailed(failure_message, kind=failure_kind)
    return valid_emails


def set_record_emails(record, emails):
    """Rank found emails and update the lead's email and review fields in place."""
    unique = {}
    for email in emails:
        if is_valid_email(email):
            unique.setdefault(email.casefold(), email)
    domain = website_domain(record.get("website"))
    ranked = sorted(unique.values(), key=lambda email: email_sort_key(email, domain))
    record["email"] = ranked[0] if ranked else ""
    record["alternate_emails"] = ";".join(ranked[1:])
    if not ranked:
        record["email_status"] = ""
        return
    record["email_status"] = email_status(ranked[0], domain)
    reasons = {
        reason.strip()
        for reason in clean_value(record.get("review_reasons")).split(";")
        if reason.strip() and reason.strip() != "email domain differs from website"
    }
    if record["email_status"] == "REVIEW":
        reasons.add("email domain differs from website")
    record["review_reasons"] = "; ".join(sorted(reasons))
    record["review_status"] = "REVIEW" if reasons else "READY"


def summarize(records, attempted, skipped_max_attempts):
    statuses = {
        status: sum(row["email_enrichment_status"] == status for row in records)
        for status in ("PENDING", "FOUND", "NOT_FOUND", "NO_WEBSITE", "FAILED")
    }
    summary = {
        "Total rows": len(records),
        "Search-only targets": sum(is_search_only(row) for row in records),
        "Attempted this run": attempted,
        "Found": statuses["FOUND"],
        "Not found": statuses["NOT_FOUND"],
        "No website": statuses["NO_WEBSITE"],
        "Failed": statuses["FAILED"],
        "Pending": statuses["PENDING"],
        "Skipped max attempts": skipped_max_attempts,
    }
    for label, value in summary.items():
        print(f"{label}: {value}")
    return summary


def run_email_enrichment(
    records,
    output_path,
    target_predicate,
    limit=None,
    timeout=15,
    verbose=False,
    retry_failed=False,
    driver_factory=None,
    scraper_factory=None,
):
    """Run the shared resumable browser loop for a selected set of records."""
    output_path = Path(output_path)
    atomic_write_csv(output_path, records)
    browser = BrowserSession(driver_factory or create_chrome_driver)
    if scraper_factory is None:
        if __package__:
            from utils.web_site_scraper import PatternScrapper
        else:
            from web_site_scraper import PatternScrapper
        scraper_factory = lambda: PatternScrapper(
            wait_time=timeout,
            overall_timeout=timeout,
            verbose=False,
        )
    scraper = scraper_factory()
    attempted = 0
    skipped_max_attempts = 0
    skipped_existing_result = 0
    found_this_run = 0
    not_found_this_run = 0
    failed_this_run = 0
    browser_exhausted = False

    try:
        for record in records:
            if not target_predicate(record):
                continue
            status = record["email_enrichment_status"]
            if status in {"FOUND", "NO_WEBSITE"}:
                continue
            if status == "FAILED" and not retry_failed:
                skipped_existing_result += 1
                continue
            attempts = parse_attempts(record["email_enrichment_attempts"])
            if attempts >= MAX_LIFETIME_ATTEMPTS:
                skipped_max_attempts += 1
                continue
            if limit is not None and attempted >= limit:
                continue

            attempted += 1
            record["email_enrichment_attempts"] = str(attempts + 1)
            try:
                driver = browser.get()
                found = extract_public_emails(scraper, driver, record["website"])
                if found:
                    set_record_emails(record, found)
                    record["email_enrichment_status"] = "FOUND"
                    found_this_run += 1
                else:
                    record["email_enrichment_status"] = "NOT_FOUND"
                    not_found_this_run += 1
            except WebsiteCheckFailed as error:
                record["email_enrichment_status"] = "FAILED"
                failed_this_run += 1
                if verbose:
                    labels = {
                        "dns": "FAILED_DNS",
                        "timeout": "FAILED_TIMEOUT",
                        "connection": "FAILED_CONNECTION",
                    }
                    label = labels.get(error.kind, "FAILED")
                    subject = record["name"] or record["website"]
                    print(f"[{label}] {subject} - {error}")
            except Exception as error:
                record["email_enrichment_status"] = "FAILED"
                failed_this_run += 1
                if verbose:
                    print(
                        f"[-] Email enrichment failed for {record['name'] or record['website']}: "
                        f"{type(error).__name__}: {error}"
                    )
                if is_driver_failure(error):
                    browser.invalidate()
                    if not browser.can_recreate():
                        browser_exhausted = True
            atomic_write_csv(output_path, records)
            if verbose and record["email_enrichment_status"] != "FAILED":
                print(f"[{record['email_enrichment_status']}] {record['name']}")
            if browser_exhausted:
                break
    finally:
        browser.close()
        atomic_write_csv(output_path, records)

    return {
        "attempted": attempted,
        "found": found_this_run,
        "not_found": not_found_this_run,
        "failed": failed_this_run,
        "skipped_existing_result": skipped_existing_result,
        "skipped_max_attempts": skipped_max_attempts,
    }


def enrich_search_emails(
    input_path=DEFAULT_INPUT,
    output_path=DEFAULT_OUTPUT,
    limit=None,
    timeout=15,
    verbose=False,
    retry_failed=False,
    driver_factory=None,
    scraper_factory=None,
):
    """Enrich eligible Search-only rows, checkpointing after every attempt."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    records = prepare_records(input_path, output_path)
    counters = run_email_enrichment(
        records=records,
        output_path=output_path,
        target_predicate=is_search_only,
        limit=limit,
        timeout=timeout,
        verbose=verbose,
        retry_failed=retry_failed,
        driver_factory=driver_factory,
        scraper_factory=scraper_factory,
    )

    return summarize(records, counters["attempted"], counters["skipped_max_attempts"])


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit < 0:
        parser.error("--limit must be zero or greater")
    if arguments.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return arguments


def main():
    arguments = parse_arguments()
    enrich_search_emails(
        input_path=arguments.input,
        output_path=arguments.output,
        limit=arguments.limit,
        timeout=arguments.timeout,
        verbose=arguments.verbose,
        retry_failed=arguments.retry_failed,
    )


if __name__ == "__main__":
    main()
