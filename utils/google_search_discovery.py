"""
Google Search Company Discovery
===============================

Purpose:
    Discover likely official company websites from ordinary Google results
    while excluding directories, social networks, job boards, and articles.

Pipeline:
    google_queries.txt -> google_search_discovery.py
                       -> google_search_companies.csv -> build_leads.py

Input:
    One search query per line plus result-limit, delay, and browser settings.

Output:
    A resumable CSV of company name, normalized website, source query, URL,
    and deterministic query-derived geographic metadata.

Previous / next:
    This is an optional discovery entry point; ``build_leads.py`` normally follows.
"""

from argparse import ArgumentParser
from csv import DictReader, DictWriter
from os import replace
from pathlib import Path
from re import IGNORECASE, compile
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.parse import quote_plus, unquote_plus, urlsplit

try:
    from utils.build_leads import clean_value, website_domain
    from utils.discovery_timestamps import discovery_timestamp, earliest_added_at
    from utils.geography import extract_queries_geography
    from utils.known_companies import KnownCompanies
except ModuleNotFoundError:  # Support direct execution from the utils directory.
    from build_leads import clean_value, website_domain
    from discovery_timestamps import discovery_timestamp, earliest_added_at
    from geography import extract_queries_geography
    from known_companies import KnownCompanies


DEFAULT_QUERY_FILE = Path("./google_queries.txt")
DEFAULT_OUTPUT = Path("./CSV_FILES/google_search_companies.csv")
DISCOVERY_FIELDS = (
    "company_name",
    "website",
    "source",
    "source_query",
    "source_url",
    "country",
    "city",
    "location",
    "added_at",
)

# ---------------------------------------------------------------------------
# OFFICIAL-WEBSITE FILTERS
# ---------------------------------------------------------------------------
# Keep this list explicit and conservative. Entries match the domain itself and
# any of its subdomains.
BLOCKED_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "reddit.com",
    "x.com",
    "twitter.com",
    "doubleclick.net",
    "googleadservices.com",
    # Job boards.
    "monster.com",
    "ziprecruiter.com",
    "wellfound.com",
    "trabajo.org",
    "meetfrank.com",
    # News and publishing platforms.
    "reuters.com",
    "bloomberg.com",
    "forbes.com",
    "medium.com",
    # Generic directories and review/aggregation sites.
    "clutch.co",
    "goodfirms.co",
    "sortlist.com",
    "crunchbase.com",
    "yellowpages.com",
    "yelp.com",
    "techbehemoths.com",
    "elioplus.com",
    "f6s.com",
    "saasdatabase.net",
    "africashore.com",
    "designrush.com",
    "upwork.com",
}

# Match these as whole domain labels so rules such as ``indeed.*`` cover
# indeed.com, indeed.fr, and their subdomains without matching a name such as
# indeedsoftware.com.
BLOCKED_DOMAIN_WILDCARDS = {
    # Job boards.
    "optioncarriere",
    "indeed",
    "glassdoor",
    "jooble",
    # Directories and marketplaces.
    "sortlist",
    "freelancer",
}

BLOCKED_PAGE_MARKERS = (
    "our systems have detected unusual traffic",
    "unusual traffic from your computer network",
    "verify you are human",
    "verify that you're not a robot",
    "recaptcha",
    "before you continue to google",
    "consent.google.com",
)

TITLE_SEPARATOR = compile(r"\s+(?:\||[-–—])\s+", flags=IGNORECASE)
PDF_PATH = compile(r"\.pdf(?:$|[?#])", flags=IGNORECASE)
AGGREGATOR_TITLE_PATTERNS = (
    compile(r"^top\s+\d+\b", IGNORECASE),
    compile(r"^top(?:\s+\d+)?\b.*\b(?:companies|firms|agencies|developers)\b", IGNORECASE),
    compile(r"^best\b.*\b(?:companies|firms|agencies|developers)\b", IGNORECASE),
    compile(r"^\d+\b.*\bsoftware\s+companies\s+in\b", IGNORECASE),
    compile(r"^(?:\d+\s+)?(?:software\s+)?companies\s+in\b", IGNORECASE),
    compile(r"^freelancers?\s+in\b", IGNORECASE),
    compile(r"^jobs?\s+in\b", IGNORECASE),
    compile(r"^offres?\s+d['’]emploi\b", IGNORECASE),
    compile(r"\bhire\s+(?:a\s+|the\s+)?developers?\b", IGNORECASE),
    compile(r"^(?:the\s+)?directory\b|\bdirectory\s+of\b", IGNORECASE),
    compile(r"^list\s+of\b", IGNORECASE),
)
ARTICLE_PATH_SEGMENTS = {
    "article",
    "articles",
    "blog",
    "blogs",
    "category",
    "companies",
    "compare",
    "comparison",
    "directory",
    "emploi",
    "jobs",
    "listing",
    "listings",
    "news",
    "search",
}
ARTICLE_SLUG_MARKERS = {
    "quelle entreprise",
}


def domain_matches(domain, blocked_domain):
    return domain == blocked_domain or domain.endswith("." + blocked_domain)


def wildcard_domain_matches(domain, blocked_label):
    return blocked_label in domain.split(".")[:-1]


def is_google_domain(domain):
    labels = domain.split(".")
    return "google" in labels or domain_matches(domain, "googleusercontent.com")


def is_suitable_company_url(url):
    """Return whether a result URL can plausibly be an official website."""
    value = clean_value(url)
    if not value or PDF_PATH.search(value):
        return False
    parsed = urlsplit(value if "://" in value else "//" + value)
    if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
        return False
    domain = website_domain(value)
    if not domain or is_google_domain(domain):
        return False
    if any(domain_matches(domain, blocked) for blocked in BLOCKED_DOMAINS):
        return False
    if any(
        wildcard_domain_matches(domain, blocked)
        for blocked in BLOCKED_DOMAIN_WILDCARDS
    ):
        return False
    return True


def has_aggregator_title(title):
    title = clean_value(title)
    return any(pattern.search(title) for pattern in AGGREGATOR_TITLE_PATTERNS)


def has_article_path(url):
    parsed = urlsplit(url if "://" in url else "//" + url)
    path = unquote_plus(parsed.path).casefold()
    segments = {segment for segment in path.split("/") if segment}
    return bool(segments & ARTICLE_PATH_SEGMENTS)


def has_known_article_slug(url):
    parsed = urlsplit(url if "://" in url else "//" + url)
    path = unquote_plus(parsed.path).casefold()
    return any(marker in path for marker in ARTICLE_SLUG_MARKERS)


def domain_is_represented_by_title(domain, title):
    """Conservatively recognize a publisher's own branded result title."""
    brand = domain.split(".", 1)[0]
    normalized_brand = "".join(character for character in brand if character.isalnum())
    title_lead = TITLE_SEPARATOR.split(clean_value(title), maxsplit=1)[0]
    normalized_title = "".join(
        character for character in title_lead.casefold() if character.isalnum()
    )
    return (
        len(normalized_brand) >= 2
        and normalized_brand == normalized_title
    )


def is_suitable_company_result(title, url):
    """Return whether URL and title evidence suggest an official company site."""
    if not is_suitable_company_url(url) or has_aggregator_title(title):
        return False
    domain = website_domain(url)
    if has_known_article_slug(url):
        return False
    if has_article_path(url) and not domain_is_represented_by_title(domain, title):
        return False
    return True


def normalized_website(url):
    """Return a deterministic origin URL using build_leads' domain identity."""
    domain = website_domain(url)
    return f"https://{domain}/" if domain else ""


def company_name_from_title(title):
    """Remove only a clear title suffix; otherwise retain the visible title."""
    cleaned = clean_value(title)
    if not cleaned:
        return ""
    first_part = TITLE_SEPARATOR.split(cleaned, maxsplit=1)[0].strip()
    return first_part if len(first_part) >= 2 else cleaned


def split_provenance(value):
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def add_unique(values, value):
    if value and value.casefold() not in {item.casefold() for item in values}:
        values.append(value)


def normalize_discovery(row):
    source_url = clean_value(row.get("source_url") or row.get("website"))
    company_name = clean_value(row.get("company_name"))
    if not is_suitable_company_result(company_name, source_url):
        return None
    source_query = ";".join(split_provenance(row.get("source_query")))
    geography = extract_queries_geography(source_query)
    return {
        "company_name": company_name_from_title(company_name),
        "website": normalized_website(source_url),
        "source": "google_search",
        "source_query": source_query,
        "source_url": source_url,
        **geography,
        "added_at": clean_value(row.get("added_at")),
    }


def merge_discoveries(existing_rows, new_rows):
    """Merge discoveries by normalized domain while preserving query evidence."""
    merged = []
    domain_indexes = {}
    for candidate in [*existing_rows, *new_rows]:
        row = normalize_discovery(candidate)
        if row is None:
            continue
        domain = website_domain(row["website"])
        index = domain_indexes.get(domain)
        if index is None:
            domain_indexes[domain] = len(merged)
            merged.append(row)
            continue
        current = merged[index]
        queries = split_provenance(current["source_query"])
        for query in split_provenance(row["source_query"]):
            add_unique(queries, query)
        current["source_query"] = ";".join(queries)
        # Re-resolve after merging provenance: compatible queries can supply a
        # missing city, while conflicting markets must remain visibly blank.
        current.update(extract_queries_geography(current["source_query"]))
        if not current["company_name"] and row["company_name"]:
            current["company_name"] = row["company_name"]
        if not current["source_url"] and row["source_url"]:
            current["source_url"] = row["source_url"]
        current["added_at"] = earliest_added_at(
            (current.get("added_at"), row.get("added_at"))
        )
    return merged


def merge_accepted_discoveries(existing_rows, candidate_rows):
    """Timestamp only domains that are absent from the durable checkpoint."""
    discoveries = merge_discoveries(existing_rows, [])
    known_domains = {website_domain(row.get("website")) for row in discoveries}
    prepared = []
    for candidate in candidate_rows:
        row = dict(candidate)
        normalized = normalize_discovery(row)
        domain = website_domain(normalized.get("website")) if normalized else ""
        if domain and domain not in known_domains:
            row["added_at"] = discovery_timestamp()
            known_domains.add(domain)
        prepared.append(row)
    return merge_discoveries(discoveries, prepared)


def read_discoveries(path):
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as file_handler:
        return list(DictReader(file_handler))


def atomic_write_discoveries(path, rows):
    """Replace the CSV atomically so an interrupted run retains prior progress."""
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
            writer = DictWriter(file_handler, fieldnames=DISCOVERY_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def load_queries(path):
    with path.open("r", encoding="utf-8-sig") as file_handler:
        return [line.strip() for line in file_handler if line.strip()]


def create_chrome_driver(windowed=False):
    from selenium import webdriver

    options = webdriver.ChromeOptions()
    if not windowed:
        options.add_argument("--headless=new")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--window-size=1440,1200")
    return webdriver.Chrome(options=options)


def blocked_search_page(driver):
    current_url = clean_value(getattr(driver, "current_url", "")).casefold()
    title = clean_value(getattr(driver, "title", "")).casefold()
    try:
        body = driver.find_element("tag name", "body").text.casefold()
    except Exception:
        body = ""
    page_text = " ".join((current_url, title, body))
    return next((marker for marker in BLOCKED_PAGE_MARKERS if marker in page_text), "")


def extract_organic_results(driver, limit, verbose=False):
    """Extract title/link pairs from rendered organic result containers."""
    discoveries = []
    seen_urls = set()
    containers = driver.find_elements("css selector", "div.MjjYud, div.g")
    for container in containers:
        if len(discoveries) >= limit:
            break
        try:
            heading = container.find_element("css selector", "h3")
            anchor = heading.find_element("xpath", "ancestor::a[1]")
            title = clean_value(heading.text)
            url = clean_value(anchor.get_attribute("href"))
            if not title or url in seen_urls or not is_suitable_company_result(title, url):
                continue
            seen_urls.add(url)
            discoveries.append({"company_name": title, "source_url": url})
        except Exception as error:
            if verbose:
                print(f"[-] Skipping unreadable result: {type(error).__name__}: {error}")
    return discoveries


def read_current_search_results(driver, query, limit, timeout, verbose=False):
    """Read results from the current page without navigating or refreshing it."""
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    WebDriverWait(driver, timeout).until(
        lambda current: current.find_elements("tag name", "body")
    )
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: (
                blocked_search_page(current)
                or current.find_elements("css selector", "div.MjjYud h3, div.g h3")
            )
        )
    except TimeoutException:
        if verbose:
            print("[-] No organic result entries appeared before the timeout")
    blocked_marker = blocked_search_page(driver)
    if blocked_marker:
        return [], blocked_marker
    rows = extract_organic_results(driver, limit, verbose=verbose)
    for row in rows:
        row.update({"source": "google_search", "source_query": query})
    return rows, ""


def search_query(driver, query, limit, timeout, verbose=False, start=0):
    driver.set_page_load_timeout(timeout)
    url = "https://www.google.com/search?q=" + quote_plus(query)
    if start:
        url += f"&start={start}"
    driver.get(url)
    return read_current_search_results(
        driver,
        query,
        limit,
        timeout,
        verbose=verbose,
    )


def has_next_search_page(driver):
    """Return whether rendered Search controls advertise another result page."""
    try:
        return bool(driver.find_elements(
            "css selector",
            "a#pnnext, a[aria-label='Next page'], a[aria-label='Next']",
        ))
    except Exception:
        return False


def wait_for_manual_verification(
    driver,
    query,
    limit,
    timeout,
    output_path,
    discoveries,
    verbose=False,
    input_function=None,
    checkpoint_function=None,
    result_reader=None,
):
    """Wait for explicit confirmation while a person clears verification."""
    input_function = input_function or input
    checkpoint_function = checkpoint_function or (
        lambda: atomic_write_discoveries(output_path, discoveries)
    )
    result_reader = result_reader or read_current_search_results
    checkpoint_function()
    print("[!] Google verification required.")
    print("[!] Solve the CAPTCHA / verification manually in the Chrome window.")
    print("[!] When normal Google Search results are visible again, return here and press ENTER.")
    print("[!] Type q then ENTER to abort safely.")

    while True:
        try:
            choice = input_function("Press ENTER to continue, or q to quit: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[!] Manual verification aborted; saved current progress.")
            checkpoint_function()
            return [], True

        if choice == "q":
            checkpoint_function()
            print("[!] Discovery stopped safely; saved current progress.")
            return [], True
        if choice:
            print("[!] Enter q to quit, or press ENTER after completing verification.")
            continue

        blocked_marker = blocked_search_page(driver)
        if blocked_marker:
            checkpoint_function()
            print(f"[!] Google verification is still present ({blocked_marker}).")
            continue

        rows, blocked_marker = result_reader(
            driver,
            query,
            limit,
            timeout,
            verbose=verbose,
        )
        if blocked_marker:
            checkpoint_function()
            print(f"[!] Google verification is still present ({blocked_marker}).")
            continue
        print("[+] Google verification cleared; resuming the current query.")
        return rows, False


def discover_companies(
    query_file=DEFAULT_QUERY_FILE,
    output_path=DEFAULT_OUTPUT,
    limit=20,
    delay=3,
    timeout=15,
    windowed=False,
    verbose=False,
    driver_factory=None,
    input_function=None,
    rebuild=False,
    incremental=False,
):
    """Run all queries, merge unique domains, and checkpoint after each query."""
    query_file = Path(query_file)
    output_path = Path(output_path)
    queries = load_queries(query_file)
    discoveries = [] if rebuild else merge_discoveries(read_discoveries(output_path), [])
    known_companies = (
        KnownCompanies.from_directory(output_path.parent) if incremental else None
    )
    overall = {"queries": 0, "inspected": 0, "known": 0, "same_run": 0, "new": 0}
    if not queries or limit == 0:
        atomic_write_discoveries(output_path, discoveries)
        return discoveries

    driver = None
    try:
        try:
            driver = (driver_factory or create_chrome_driver)(windowed=windowed)
        except Exception as error:
            print(f"[-] Browser unavailable: {type(error).__name__}: {error}")
            return discoveries
        for query_index, query in enumerate(queries):
            if verbose:
                print(f"[+] Searching: {query}")
            try:
                query_stats = {"inspected": 0, "known": 0, "same_run": 0, "new": 0}
                page_start = 0
                page_attempts = 0
                inspection_limit = max(100, limit * 10) if incremental else limit
                should_stop_all = False
                while (
                    query_stats["inspected"] < inspection_limit
                    and (not incremental or page_attempts * 10 < inspection_limit)
                ):
                    page_attempts += 1
                    page_limit = (
                        min(10, inspection_limit - query_stats["inspected"])
                        if incremental else limit
                    )
                    rows, blocked_marker = search_query(
                        driver, query, page_limit,
                        timeout, verbose=verbose, start=page_start,
                    )
                    if blocked_marker:
                        atomic_write_discoveries(output_path, discoveries)
                        if not windowed:
                            print(
                                "[!] Google verification detected; manual verification "
                                "requires --windowed. Saved progress and stopped safely."
                            )
                            should_stop_all = True
                            break
                        rows, should_stop = wait_for_manual_verification(
                            driver, query, page_limit, timeout,
                            output_path, discoveries, verbose=verbose,
                            input_function=input_function,
                        )
                        if should_stop:
                            should_stop_all = True
                            break

                    if not incremental:
                        discoveries = merge_accepted_discoveries(discoveries, rows)
                        atomic_write_discoveries(output_path, discoveries)
                        query_stats["inspected"] += len(rows)
                        query_stats["new"] += len(rows)
                        break

                    if not rows:
                        if has_next_search_page(driver):
                            page_start += 10
                            continue
                        break
                    for row in rows:
                        if query_stats["inspected"] >= inspection_limit:
                            break
                        query_stats["inspected"] += 1
                        duplicate_kind = known_companies.duplicate_kind(row)
                        if duplicate_kind:
                            query_stats[duplicate_kind] += 1
                            continue
                        if query_stats["new"] >= limit:
                            break
                        accepted_row = dict(row)
                        accepted_row["added_at"] = discovery_timestamp()
                        updated = merge_discoveries(discoveries, [accepted_row])
                        if len(updated) == len(discoveries):
                            query_stats["same_run"] += 1
                            known_companies.add(row)
                            continue
                        # Checkpoint every accepted company. Only after the
                        # atomic replace succeeds does it become known in memory.
                        atomic_write_discoveries(output_path, updated)
                        discoveries = updated
                        known_companies.add(row)
                        query_stats["new"] += 1
                    if query_stats["new"] >= limit:
                        break
                    if len(rows) < min(10, inspection_limit) and not has_next_search_page(driver):
                        break
                    page_start += 10

                if incremental:
                    overall["queries"] += 1
                    for key in ("inspected", "known", "same_run", "new"):
                        overall[key] += query_stats[key]
                    print(f"Query: {query}")
                    print(f"Inspected results: {query_stats['inspected']}")
                    print(f"Already known skipped: {query_stats['known']}")
                    print(f"Same-run duplicates skipped: {query_stats['same_run']}")
                    print(f"New companies added: {query_stats['new']}")
                if should_stop_all:
                    break
                if verbose:
                    print(f"[+] Stored {len(discoveries)} unique company domains")
            except Exception as error:
                print(f"[-] Query failed ({query}): {type(error).__name__}: {error}")
            if query_index + 1 < len(queries) and delay:
                sleep(delay)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    if incremental:
        print("Overall:")
        print(f"Queries processed: {overall['queries']}")
        print(f"Results inspected: {overall['inspected']}")
        print(f"Known duplicates skipped: {overall['known']}")
        print(f"Same-run duplicates skipped: {overall['same_run']}")
        print(f"New companies added: {overall['new']}")
    return discoveries


def parse_arguments():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--query-file", type=Path, default=DEFAULT_QUERY_FILE)
    parser.add_argument("-l", "--limit", type=int, default=20)
    parser.add_argument("--delay", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--incremental", action="store_true",
        help="Make --limit count only new domains and continue through result pages",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Ignore existing discoveries and rebuild the output from this run",
    )
    arguments = parser.parse_args()
    if arguments.limit < 0:
        parser.error("--limit must be zero or greater")
    if arguments.delay < 0:
        parser.error("--delay must be zero or greater")
    if arguments.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if not arguments.query_file.is_file():
        parser.error(f"query file not found: {arguments.query_file}")
    return arguments


def main():
    arguments = parse_arguments()
    discoveries = discover_companies(
        query_file=arguments.query_file,
        output_path=arguments.output,
        limit=arguments.limit,
        delay=arguments.delay,
        timeout=arguments.timeout,
        windowed=arguments.windowed,
        verbose=arguments.verbose,
        rebuild=arguments.rebuild,
        incremental=arguments.incremental,
    )
    print(f"Unique company domains: {len(discoveries)}")
    print(f"Output: {arguments.output}")


if __name__ == "__main__":
    main()
