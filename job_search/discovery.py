"""Discover job pages with Google Search and persist them incrementally."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from pathlib import Path
from time import sleep
from urllib.parse import urlsplit

from job_search.providers import (
    JobResultClassification,
    classify_job_result,
    fetch_job,
    generic_listing_reason,
)
from job_search.storage import DEFAULT_DATABASE, connect_database, upsert_job
from utils.google_search_client import (
    extract_organic_results,
    resolve_google_result_url,
    unwrap_google_result_url,
)
from utils.google_search_discovery import (
    blocked_search_page,
    create_chrome_driver,
    has_next_search_page,
    load_queries,
    wait_for_manual_verification,
)


DEFAULT_QUERY_FILE = Path("google_queries.txt")


def read_current_search_results(
    driver, query, limit, timeout, verbose=False, diagnose_results=False,
    resolution_cache=None,
):
    """Read unfiltered organic results so rejected/noise totals stay accurate."""
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    WebDriverWait(driver, timeout).until(
        lambda current: current.find_elements("tag name", "body")
    )
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: blocked_search_page(current)
            or current.find_elements("css selector", "div.MjjYud h3, div.g h3")
        )
    except TimeoutException:
        if verbose:
            print("[-] No organic result entries appeared before the timeout")
    marker = blocked_search_page(driver)
    if marker:
        return [], marker
    rows = extract_organic_results(
        driver, limit, verbose=verbose, include_incomplete=True,
        include_diagnostics=diagnose_results,
        resolution_timeout=min(3.0, max(0.5, timeout)),
        resolution_cache=resolution_cache,
        source_query=query,
        browser_resolve_goto=True,
    )
    for row in rows:
        row["source_query"] = query
    return rows, ""


def search_query(
    driver, query, limit, timeout, verbose=False, start=0,
    diagnose_results=False, resolution_cache=None,
):
    from urllib.parse import quote_plus

    driver.set_page_load_timeout(timeout)
    url = "https://www.google.com/search?q=" + quote_plus(query)
    if start:
        url += f"&start={start}"
    driver.get(url)
    return read_current_search_results(
        driver, query, limit, timeout, verbose,
        diagnose_results=diagnose_results,
        resolution_cache=resolution_cache,
    )


def empty_stats():
    return {
        "queries": 0,
        "inspected": 0,
        "candidates": 0,
        "known": 0,
        "new": 0,
        "rejected": 0,
        "rejection_reasons": Counter(),
        "rejection_examples": defaultdict(list),
        "providers": Counter(),
        "stopped": False,
    }


def record_rejection(stats, classification, title, raw_url, verbose=False):
    reason = classification.rejection_reason or "UNKNOWN"
    stats["rejected"] += 1
    stats["rejection_reasons"][reason] += 1
    examples = stats["rejection_examples"][reason]
    if len(examples) >= 3:
        return
    example = {
        "title": (title or "").strip(),
        "url": (raw_url or "").strip(),
        "normalized_url": classification.normalized_url,
    }
    examples.append(example)
    if verbose:
        print(f"[REJECTED_{reason}]")
        print(f"title={example['title']}")
        print(f"RAW URL: {example['url']}")
        print(f"TARGET URL: {example['normalized_url']}")
        print(f"PROVIDER: {classification.provider}")
        print("CANDIDATE: no")


def print_diagnostic_result(index, row, classification):
    href_property = (row.get("href_property") or row.get("raw_url") or "").strip()
    parsed = urlsplit(href_property)
    print(f"RESULT {index}")
    print(f"TITLE: {(row.get('title') or '').strip()}")
    print(f"href property: {href_property}")
    print(f"raw href attribute: {(row.get('href_attribute') or '').strip()}")
    print(f"outerHTML snippet: {row.get('outer_html') or ''}")
    print(f"parsed hostname: {parsed.hostname or ''}")
    print(f"parsed path: {parsed.path}")
    print(f"local unwrap result: {unwrap_google_result_url(href_property)}")
    print(f"RAW HREF: {row.get('raw_url') or href_property}")
    print(f"HTTP RESOLUTION: {row.get('http_resolution') or '-'}")
    print(f"DISPLAYED DOMAIN: {row.get('displayed_domain') or '-'}")
    print("DIRECT-NAV RESOLUTION: -")
    print(f"CLICK RESOLUTION: {row.get('click_resolution') or '-'}")
    print(f"FINAL TARGET: {row.get('url') or ''}")
    print(f"PROVIDER: {classification.provider}")
    print(f"CANDIDATE: {'yes' if classification.accepted else 'no'}")
    print(f"REASON: {classification.rejection_reason or '-'}")


def discover_jobs(
    query_file=DEFAULT_QUERY_FILE,
    database=DEFAULT_DATABASE,
    limit=20,
    delay=3,
    timeout=15,
    windowed=False,
    verbose=False,
    driver_factory=None,
    fetcher=None,
    input_function=None,
    diagnose_results=False,
):
    """Find at most ``limit`` new jobs per query, with a bounded page scan."""
    queries = load_queries(Path(query_file))
    stats = empty_stats()
    connection = None if diagnose_results else connect_database(database)
    if not queries or (limit == 0 and not diagnose_results):
        if connection is not None:
            connection.close()
        return stats

    driver = None
    fetcher = fetcher or fetch_job
    resolution_cache = {}
    try:
        try:
            driver = (driver_factory or create_chrome_driver)(windowed=windowed)
        except Exception as error:
            print(f"[-] Browser unavailable: {type(error).__name__}: {error}")
            return stats

        if diagnose_results:
            from inspect import getsourcefile

            print(f"search client module: {getsourcefile(extract_organic_results)}")
            print(f"unwrap helper module: {getsourcefile(unwrap_google_result_url)}")
            print(f"redirect resolver module: {getsourcefile(resolve_google_result_url)}")

        selected_queries = queries[:1] if diagnose_results else queries
        for query_index, query in enumerate(selected_queries):
            stats["queries"] += 1
            query_new = 0
            query_inspected = 0
            page_start = 0
            inspection_limit = (
                min(10, max(1, limit))
                if diagnose_results
                else max(100, limit * 10)
            )
            if verbose:
                print(f"[+] Searching jobs: {query}")

            while query_inspected < inspection_limit and (
                diagnose_results or query_new < limit
            ):
                page_limit = min(10, inspection_limit - query_inspected)
                try:
                    rows, marker = search_query(
                        driver, query, page_limit, timeout,
                        verbose=verbose, start=page_start,
                        diagnose_results=diagnose_results,
                        resolution_cache=resolution_cache,
                    )
                except Exception as error:
                    print(f"[-] Search page failed ({query}): {type(error).__name__}: {error}")
                    break

                if marker:
                    if connection is not None:
                        connection.commit()
                    if not windowed:
                        suffix = (
                            "Diagnostic stopped without database writes."
                            if diagnose_results
                            else "Database is current; stopped safely."
                        )
                        print(
                            "[!] Google verification detected; manual verification "
                            f"requires --windowed. {suffix}"
                        )
                        stats["stopped"] = True
                        break
                    rows, should_stop = wait_for_manual_verification(
                        driver, query, page_limit, timeout, Path(database), [],
                        verbose=verbose,
                        input_function=input_function,
                        checkpoint_function=(
                            connection.commit if connection is not None else lambda: None
                        ),
                        result_reader=lambda *args, **kwargs: read_current_search_results(
                            *args, **kwargs, diagnose_results=diagnose_results,
                            resolution_cache=resolution_cache,
                        ),
                    )
                    if should_stop:
                        stats["stopped"] = True
                        break

                if not rows:
                    if diagnose_results:
                        break
                    if has_next_search_page(driver):
                        page_start += 10
                        continue
                    break

                for row in rows:
                    if query_inspected >= inspection_limit or (
                        not diagnose_results and query_new >= limit
                    ):
                        break
                    query_inspected += 1
                    stats["inspected"] += 1
                    title = row.get("title", "")
                    url = row.get("url", "")
                    raw_url = row.get("raw_url", url)
                    resolution_error = row.get("resolution_error", "")
                    classification = (
                        JobResultClassification(
                            False, rejection_reason=resolution_error
                        )
                        if resolution_error
                        else classify_job_result(title, url)
                    )
                    if diagnose_results:
                        print_diagnostic_result(
                            query_inspected, row, classification
                        )
                    if resolution_error == "GOOGLE_GOTO_VERIFICATION_REQUIRED":
                        if connection is not None:
                            connection.commit()
                        print(
                            "[!] Google verification appeared while resolving a "
                            "/goto result; stopped safely. Complete verification "
                            "in a normal Search tab, then retry."
                        )
                        stats["stopped"] = True
                        record_rejection(
                            stats, classification, title, raw_url, verbose=verbose
                        )
                        break
                    if not classification.accepted:
                        record_rejection(
                            stats, classification, title, raw_url, verbose=verbose
                        )
                        continue
                    stats["candidates"] += 1
                    if diagnose_results:
                        continue
                    canonical = classification.normalized_url
                    provider = classification.provider
                    parsed = fetcher(canonical, timeout=timeout)
                    if not parsed.title:
                        parsed.title = title.strip()
                    post_fetch_listing = generic_listing_reason(
                        parsed.title, canonical, parsed.description
                    )
                    if post_fetch_listing:
                        record_rejection(
                            stats,
                            JobResultClassification(
                                False, canonical, provider, post_fetch_listing
                            ),
                            parsed.title,
                            raw_url,
                            verbose=verbose,
                        )
                        continue
                    _, was_new = upsert_job(connection, parsed, query)
                    if was_new:
                        query_new += 1
                        stats["new"] += 1
                        stats["providers"][provider] += 1
                    else:
                        stats["known"] += 1

                if diagnose_results or query_new >= limit:
                    break
                if stats["stopped"]:
                    break
                if len(rows) < page_limit and not has_next_search_page(driver):
                    break
                page_start += 10

            if stats["stopped"]:
                break
            if verbose:
                print(f"[+] Query stored {query_new} new jobs after inspecting {query_inspected} results")
            if query_index + 1 < len(selected_queries) and delay:
                sleep(delay)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if connection is not None:
            connection.close()
    return stats


def print_summary(stats, database, diagnose_results=False):
    print(f"Queries processed: {stats['queries']}")
    print(f"Search results inspected: {stats['inspected']}")
    print(f"Job candidates: {stats['candidates']}")
    print(f"Known jobs: {stats['known']}")
    print(f"New jobs: {stats['new']}")
    print(f"Rejected/noise: {stats['rejected']}")
    print("Rejection reasons:")
    for reason, count in sorted(stats["rejection_reasons"].items()):
        print(f"  {reason}: {count}")
    print("By provider (new jobs):")
    for provider in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "teamtailor", "generic"):
        if stats["providers"][provider]:
            print(f"{provider.title()}: {stats['providers'][provider]}")
    if diagnose_results:
        print("Database: not written (--diagnose-results)")
    else:
        print(f"Database: {database}")


def parse_arguments(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--query-file", type=Path, default=DEFAULT_QUERY_FILE)
    parser.add_argument("-l", "--limit", type=int, default=20,
                        help="Maximum NEW jobs stored per query")
    parser.add_argument("--delay", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--diagnose-results", action="store_true",
        help="Inspect one query/first page without fetching or writing jobs",
    )
    arguments = parser.parse_args(argv)
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
    stats = discover_jobs(
        query_file=arguments.query_file,
        database=arguments.database,
        limit=arguments.limit,
        delay=arguments.delay,
        timeout=arguments.timeout,
        windowed=arguments.windowed,
        verbose=arguments.verbose,
        diagnose_results=arguments.diagnose_results,
    )
    print_summary(stats, arguments.database, arguments.diagnose_results)


if __name__ == "__main__":
    main()
