"""Bounded Google Search fallback for leads still missing a valid email.

Only leads whose direct website enrichment ended in ``NOT_FOUND`` or
``FAILED`` are considered. Progress is atomically checkpointed after every
company and all accepted addresses pass the existing production validator.
"""

from argparse import ArgumentParser
from csv import DictReader, DictWriter
from os import replace
from pathlib import Path
from re import IGNORECASE, compile
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import quote_plus, urlsplit

if __package__:
    from utils.build_leads import (
        clean_value,
        email_matches_website,
        email_sort_key,
        extract_emails,
        is_valid_email,
        website_domain,
    )
    from utils.enrich_leads import BrowserSession, is_driver_failure
    from utils.enrich_search_emails import parse_attempts, valid_row_emails
    from utils.google_search_discovery import (
        blocked_search_page,
        create_chrome_driver,
        is_suitable_company_url,
        wait_for_manual_verification,
    )
else:  # Support direct execution from the utils directory.
    from build_leads import (
        clean_value,
        email_matches_website,
        email_sort_key,
        extract_emails,
        is_valid_email,
        website_domain,
    )
    from enrich_leads import BrowserSession, is_driver_failure
    from enrich_search_emails import parse_attempts, valid_row_emails
    from google_search_discovery import (
        blocked_search_page,
        create_chrome_driver,
        is_suitable_company_url,
        wait_for_manual_verification,
    )


DEFAULT_INPUT = Path("./CSV_FILES/leads_master.csv")
DEFAULT_ENRICHMENT_INPUT = Path("./CSV_FILES/missing_email_enriched.csv")
DEFAULT_OUTPUT = Path("./CSV_FILES/search_email_fallback.csv")
MAX_LIFETIME_ATTEMPTS = 3
MAX_QUERIES_PER_COMPANY = 4
MAX_RESULTS_PER_QUERY = 5
MAX_PAGE_OPENS_PER_COMPANY = 2
OUTPUT_FIELDS = (
    "company_name",
    "website",
    "normalized_domain",
    "email",
    "source_query",
    "source_url",
    "status",
    "attempts",
)
CONTACT_MARKER = compile(
    r"(?:^|[\W_])(contact|contacts|about)(?:$|[\W_])", flags=IGNORECASE
)
NAME_WORDS = compile(r"[a-z0-9]+")
TITLE_SEPARATOR = compile(r"\s+(?:\||[-–—])\s+")


class CompanyDeadlineExceeded(TimeoutError):
    """Raised before an operation that would exceed the company time budget."""


class GoogleVerificationStopped(RuntimeError):
    """Stop the run when verification cannot be completed safely."""


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(DictReader(handle))


def atomic_write_csv(path, rows):
    """Atomically replace fallback state, preserving progress on interruption."""
    path = Path(path)
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
        ) as handle:
            temporary_path = Path(handle.name)
            writer = DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def company_name(row):
    return clean_value(row.get("company_name") or row.get("name") or row.get("title"))


def company_website(row):
    return clean_value(row.get("website") or row.get("webpage"))


def normalized_name(value):
    return " ".join(NAME_WORDS.findall(clean_value(value).casefold()))


def row_identity(row, row_index=0):
    domain = clean_value(row.get("normalized_domain")) or website_domain(company_website(row))
    if domain:
        return "domain", domain.casefold()
    name = normalized_name(company_name(row))
    return ("name", name) if name else ("row", str(row_index))


def enrichment_status(row):
    return clean_value(row.get("email_enrichment_status") or row.get("status")).upper()


def enrichment_index(rows):
    return {row_identity(row, index): row for index, row in enumerate(rows)}


def prepare_records(master_rows, enrichment_rows, previous_rows):
    """Return only fallback state, while retaining prior valid FOUND records."""
    enrichment = enrichment_index(enrichment_rows)
    previous = enrichment_index(previous_rows)
    records = []
    seen = set()
    eligible_count = 0

    for index, lead in enumerate(master_rows):
        identity = row_identity(lead, index)
        if identity in seen:
            continue
        seen.add(identity)
        prior = previous.get(identity, {})
        prior_email = next(iter(valid_row_emails(prior)), "")
        prior_found = enrichment_status(prior) == "FOUND" and bool(prior_email)
        website = company_website(lead)
        domain = website_domain(website)
        direct_status = enrichment_status(enrichment.get(identity, {}))
        eligible = (
            not valid_row_emails(lead)
            and bool(domain)
            and direct_status in {"NOT_FOUND", "FAILED"}
        )
        if not eligible and not prior_found:
            continue
        if eligible:
            eligible_count += 1

        attempts = parse_attempts(prior.get("attempts"))
        status = "FOUND" if prior_found else clean_value(prior.get("status")).upper()
        if status not in {"PENDING", "FOUND", "NOT_FOUND", "FAILED"}:
            status = "PENDING"
        records.append({
            "company_name": company_name(lead) or company_name(prior),
            "website": website or company_website(prior),
            "normalized_domain": domain or clean_value(prior.get("normalized_domain")),
            "email": prior_email if prior_found else "",
            "source_query": clean_value(prior.get("source_query")) if prior_found else "",
            "source_url": clean_value(prior.get("source_url")) if prior_found else "",
            "status": status,
            "attempts": str(attempts),
        })
    return records, eligible_count


def build_queries(name, domain):
    """Build four narrow queries, prioritizing the known company domain."""
    queries = []
    if domain:
        queries.extend((f"site:{domain} email", f"site:{domain} contact"))
    if name:
        escaped_name = name.replace('"', " ").strip()
        queries.extend((f'"{escaped_name}" email', f'"{escaped_name}" contact'))
    return queries[:MAX_QUERIES_PER_COMPANY]


def read_current_fallback_results(driver, query, limit, timeout, verbose=False):
    """Read organic title, URL, and visible snippet text from the current page."""
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    read_deadline = monotonic() + timeout
    WebDriverWait(driver, timeout).until(
        lambda current: current.find_elements("tag name", "body")
    )
    remaining = read_deadline - monotonic()
    if remaining <= 0:
        raise CompanyDeadlineExceeded("search result deadline exceeded")
    try:
        WebDriverWait(driver, remaining).until(
            lambda current: blocked_search_page(current)
            or current.find_elements("css selector", "div.MjjYud h3, div.g h3")
        )
    except TimeoutException:
        if verbose:
            print("[-] No organic result entries appeared before the timeout")
    marker = blocked_search_page(driver)
    if marker:
        return [], marker

    results = []
    seen = set()
    for container in driver.find_elements("css selector", "div.MjjYud, div.g"):
        if len(results) >= limit:
            break
        try:
            heading = container.find_element("css selector", "h3")
            anchor = heading.find_element("xpath", "ancestor::a[1]")
            title = clean_value(heading.text)
            url = clean_value(anchor.get_attribute("href"))
            if not title or url in seen or not is_suitable_company_url(url):
                continue
            seen.add(url)
            results.append({"title": title, "url": url, "snippet": clean_value(container.text)})
        except Exception as error:
            if verbose:
                print(f"[-] Skipping unreadable result: {type(error).__name__}: {error}")
    return results, ""


def search_query(driver, query, limit, timeout, verbose=False):
    query_deadline = monotonic() + timeout
    driver.set_page_load_timeout(timeout)
    driver.get("https://www.google.com/search?q=" + quote_plus(query))
    remaining = query_deadline - monotonic()
    if remaining <= 0:
        raise CompanyDeadlineExceeded("search query deadline exceeded")
    return read_current_fallback_results(
        driver, query, limit, remaining, verbose=verbose
    )


def title_matches_company(title, name):
    """Use exact normalized title segments; intentionally do not fuzzy-match."""
    expected = normalized_name(name)
    if not expected:
        return False
    segments = TITLE_SEPARATOR.split(clean_value(title))
    return expected in {normalized_name(segment) for segment in segments}


def is_contact_result(result):
    parsed = urlsplit(result.get("url", ""))
    return bool(CONTACT_MARKER.search(f"{parsed.path} {result.get('title', '')}"))


def result_evidence(result, name, domain):
    if not is_suitable_company_url(result.get("url", "")):
        return {
            "relevant": False,
            "domain_match": False,
            "title_match": False,
            "contact_page": False,
        }
    result_domain = website_domain(result.get("url"))
    domain_match = bool(
        domain and result_domain
        and (
            result_domain == domain
            or result_domain.endswith("." + domain)
            or domain.endswith("." + result_domain)
        )
    )
    title_match = title_matches_company(result.get("title", ""), name)
    contact_page = is_contact_result(result)
    return {
        "relevant": domain_match or title_match,
        "domain_match": domain_match,
        "title_match": title_match,
        "contact_page": contact_page,
    }


def accepted_result_emails(result, name, domain, emails):
    """Validate candidates and require explicit company/domain evidence."""
    evidence = result_evidence(result, name, domain)
    if not evidence["relevant"]:
        return []
    accepted = []
    for email in emails:
        if not is_valid_email(email):
            continue
        email_domain = email.rsplit("@", 1)[1].casefold().rstrip(".")
        if email_matches_website(email_domain, domain):
            accepted.append(email)
        elif evidence["domain_match"] and (
            evidence["title_match"] or evidence["contact_page"]
        ):
            # A public free/mismatched address needs two independent signals:
            # the official domain plus an exact company title or contact page.
            accepted.append(email)
    return accepted


def extract_result_page_emails(scraper, driver, url):
    """Open exactly one discovered page and reuse PatternScrapper extraction."""
    sources = scraper.get_source_code(driver, [url])
    if not sources:
        return [], getattr(scraper, "last_failure_kind", None)
    emails = scraper.get_pattern_data(sources).get("site_email", [])
    return [email for email in emails if is_valid_email(email)], None


def remaining_time(deadline, clock):
    remaining = deadline - clock()
    if remaining <= 0:
        raise CompanyDeadlineExceeded("overall company deadline exceeded")
    return remaining


def failure_kind(error):
    message = " ".join(str(error).split()).upper()
    if (
        isinstance(error, (TimeoutError, CompanyDeadlineExceeded))
        or type(error).__name__ == "TimeoutException"
        or "TIMED OUT" in message
    ):
        return "timeout"
    if "ERR_NAME_NOT_RESOLVED" in message or "COULD NOT BE RESOLVED" in message:
        return "dns"
    return "other"


def process_record(
    record,
    driver,
    timeout,
    windowed,
    verbose,
    output_path,
    records,
    search_function,
    scraper_factory,
    input_function,
    clock,
):
    """Process one lead inside a single deadline and return failure metadata."""
    deadline = clock() + timeout
    name = record["company_name"]
    domain = record["normalized_domain"]
    page_opens = 0
    last_failure = None

    for query in build_queries(name, domain):
        query_timeout = remaining_time(deadline, clock)
        results, marker = search_function(
            driver, query, MAX_RESULTS_PER_QUERY, query_timeout, verbose=verbose
        )
        if marker:
            if not windowed:
                raise GoogleVerificationStopped(
                    "Google verification requires --windowed for manual completion"
                )
            pause_started = clock()
            results, should_stop = wait_for_manual_verification(
                driver,
                query,
                MAX_RESULTS_PER_QUERY,
                query_timeout,
                output_path,
                records,
                verbose=verbose,
                input_function=input_function,
                checkpoint_function=lambda: atomic_write_csv(output_path, records),
                result_reader=read_current_fallback_results,
            )
            deadline += max(0, clock() - pause_started)
            if should_stop:
                raise GoogleVerificationStopped("manual Google verification aborted")

        for result in results:
            evidence = result_evidence(result, name, domain)
            if not evidence["relevant"]:
                continue
            snippet_emails = extract_emails(result.get("snippet", ""))
            accepted = accepted_result_emails(result, name, domain, snippet_emails)
            if accepted:
                selected = sorted(set(accepted), key=lambda item: email_sort_key(item, domain))[0]
                record.update({
                    "email": selected,
                    "source_query": query,
                    "source_url": result.get("url", ""),
                    "status": "FOUND",
                })
                return None

            if not evidence["contact_page"] or page_opens >= MAX_PAGE_OPENS_PER_COMPANY:
                continue
            page_opens += 1
            page_timeout = remaining_time(deadline, clock)
            scraper = scraper_factory(page_timeout)
            page_emails, page_failure = extract_result_page_emails(
                scraper, driver, result.get("url", "")
            )
            if page_failure:
                last_failure = page_failure
            accepted = accepted_result_emails(result, name, domain, page_emails)
            if accepted:
                selected = sorted(set(accepted), key=lambda item: email_sort_key(item, domain))[0]
                record.update({
                    "email": selected,
                    "source_query": query,
                    "source_url": result.get("url", ""),
                    "status": "FOUND",
                })
                return None
    record["status"] = "FAILED" if last_failure else "NOT_FOUND"
    return last_failure


def summarize(records, eligible_count, counters):
    summary = {
        "Eligible fallback leads": eligible_count,
        "Attempted this run": counters["attempted"],
        "Found": counters["found"],
        "Not found": counters["not_found"],
        "Failed": counters["failed"],
        "Skipped already found": counters["skipped_found"],
        "Skipped max attempts": counters["skipped_max"],
    }
    for label, value in summary.items():
        print(f"{label}: {value}")
    rate = counters["found"] / counters["attempted"] * 100 if counters["attempted"] else 0.0
    summary["Recovery rate"] = rate
    print(f"Recovery rate: {rate:.1f}%")
    return summary


def search_email_fallback(
    input_path=DEFAULT_INPUT,
    enrichment_input_path=DEFAULT_ENRICHMENT_INPUT,
    output_path=DEFAULT_OUTPUT,
    limit=None,
    timeout=12,
    windowed=False,
    verbose=False,
    retry_failed=False,
    driver_factory=None,
    search_function=None,
    scraper_factory=None,
    input_function=None,
    clock=monotonic,
):
    """Run resumable bounded Google discovery for direct-enrichment misses."""
    input_path = Path(input_path)
    enrichment_input_path = Path(enrichment_input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")
    if not enrichment_input_path.exists():
        raise FileNotFoundError(f"Enrichment input CSV not found: {enrichment_input_path}")

    records, eligible_count = prepare_records(
        read_csv(input_path), read_csv(enrichment_input_path), read_csv(output_path)
    )
    atomic_write_csv(output_path, records)
    counters = {
        "attempted": 0, "found": 0, "not_found": 0, "failed": 0,
        "skipped_found": 0, "skipped_max": 0,
    }
    search_function = search_function or search_query
    if scraper_factory is None:
        if __package__:
            from utils.web_site_scraper import PatternScrapper
        else:
            from web_site_scraper import PatternScrapper
        scraper_factory = lambda remaining: PatternScrapper(
            wait_time=max(1, min(timeout, remaining)),
            overall_timeout=remaining,
            verbose=False,
        )
    browser = BrowserSession(
        driver_factory or (lambda: create_chrome_driver(windowed=windowed))
    )

    try:
        for record in records:
            if record["status"] == "FOUND" and is_valid_email(record["email"]):
                counters["skipped_found"] += 1
                continue
            if record["status"] == "FAILED" and not retry_failed:
                continue
            attempts = parse_attempts(record["attempts"])
            if attempts >= MAX_LIFETIME_ATTEMPTS:
                counters["skipped_max"] += 1
                continue
            if limit is not None and counters["attempted"] >= limit:
                continue

            counters["attempted"] += 1
            record["attempts"] = str(attempts + 1)
            diagnostic = None
            stop_after_company = False
            try:
                diagnostic = process_record(
                    record,
                    browser.get(),
                    timeout,
                    windowed,
                    verbose,
                    output_path,
                    records,
                    search_function,
                    scraper_factory,
                    input_function,
                    clock,
                )
            except Exception as error:
                record["status"] = "FAILED"
                diagnostic = failure_kind(error)
                stop_after_company = isinstance(error, GoogleVerificationStopped)
                if is_driver_failure(error):
                    browser.invalidate()

            if record["status"] == "FOUND":
                counters["found"] += 1
                label = "FOUND_SEARCH"
            elif record["status"] == "NOT_FOUND":
                counters["not_found"] += 1
                label = "NOT_FOUND"
            else:
                counters["failed"] += 1
                label = {"timeout": "FAILED_TIMEOUT", "dns": "FAILED_DNS"}.get(
                    diagnostic, "FAILED"
                )
            atomic_write_csv(output_path, records)
            if verbose:
                print(f"[{label}] {record['company_name']}")
            if stop_after_company:
                break
    finally:
        browser.close()
        atomic_write_csv(output_path, records)
    return summarize(records, eligible_count, counters)


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--enrichment-input", type=Path, default=DEFAULT_ENRICHMENT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--windowed", action="store_true")
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
    search_email_fallback(
        input_path=arguments.input,
        enrichment_input_path=arguments.enrichment_input,
        output_path=arguments.output,
        limit=arguments.limit,
        timeout=arguments.timeout,
        windowed=arguments.windowed,
        verbose=arguments.verbose,
        retry_failed=arguments.retry_failed,
    )


if __name__ == "__main__":
    main()
