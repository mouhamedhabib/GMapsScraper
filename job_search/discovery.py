"""Discover job pages with Google Search and persist them incrementally."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.parse import urlsplit

from job_search.providers import (
    JobResultClassification,
    classify_job_result,
    classify_source_quality,
    extract_source_job_id,
    fetch_job,
    generic_listing_reason,
)
from job_search.storage import (
    DEFAULT_DATABASE,
    connect_database,
    find_existing_job,
    record_job_rediscovery,
    upsert_job,
)
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


def classify_query(query):
    """Classify user intent for reporting only."""
    folded = " ".join((query or "").casefold().split())
    if re.search(
        r"(?:^|\s)site:(?:job-boards\.greenhouse\.io|boards\.greenhouse\.io|"
        r"jobs\.lever\.co|jobs\.ashbyhq\.com|apply\.workable\.com|"
        r"jobs\.workable\.com|jobs\.smartrecruiters\.com|"
        r"careers\.smartrecruiters\.com)(?:\s|$)", folded,
    ):
        return "ATS_SCOPED"
    if re.search(r"(?:^|\s)site:(?:careers?|jobs?)\.[^\s]+", folded):
        return "DIRECT_CAREERS"
    if re.search(
        r"\b(?:nestjs|fastapi|laravel|react|django|python|java|typescript|"
        r"javascript|node(?:\.js|js)?)\b", folded,
    ):
        return "ROLE_TECH"
    if re.search(
        r"\b(?:tunisia|france|belgium|switzerland|europe|paris|tunis|remote)\b",
        folded,
    ) and re.search(r"\b(?:developer|engineer|developpeur|développeur)\b", folded):
        return "ROLE_LOCATION"
    return "GENERIC"


def inspection_cap(limit):
    """Bound result inspection while leaving room for known/noise results."""
    return max(50, limit * 20)


def _source_evidence(row):
    return {
        name: (row.get(name) or "").strip()
        for name in (
            "result_snippet", "displayed_domain", "google_result_date_text",
        )
    }


def _job_identity(provider, canonical):
    source_job_id = extract_source_job_id(provider, canonical)
    return (
        (provider, source_job_id) if source_job_id
        else ("url", canonical)
    ), source_job_id


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
    diagnose_results=False, resolution_cache=None, recent_days=None,
):
    from urllib.parse import quote_plus

    driver.set_page_load_timeout(timeout)
    url = "https://www.google.com/search?q=" + quote_plus(query)
    if start:
        url += f"&start={start}"
    if recent_days is not None:
        url += f"&tbs=qdr:d{recent_days}"
    driver.get(url)
    return read_current_search_results(
        driver, query, limit, timeout, verbose,
        diagnose_results=diagnose_results,
        resolution_cache=resolution_cache,
    )


def empty_stats():
    return {
        "queries": 0,
        "pages": 0,
        "inspected": 0,
        "candidates": 0,
        "known": 0,
        "new": 0,
        "rejected": 0,
        "rejection_reasons": Counter(),
        "rejection_examples": defaultdict(list),
        "providers": Counter(),
        "source_qualities": Counter(),
        "query_categories": Counter(),
        "resolution_failures": 0,
        "browser_resolutions": 0,
        "http_job_fetches": 0,
        "known_job_early_skips": 0,
        "query_stats": [],
        # Exact membership for orchestration. Existing callers may ignore this.
        "job_states": {},
        "stopped": False,
    }


def record_rejection(
    stats, classification, title, raw_url, verbose=False, query_stats=None,
):
    reason = classification.rejection_reason or "UNKNOWN"
    stats["rejected"] += 1
    if query_stats is not None:
        query_stats["rejected"] += 1
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
    recent_days=None,
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
    run_jobs = {}
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
            started = monotonic()
            stats["queries"] += 1
            category = classify_query(query)
            stats["query_categories"][category] += 1
            query_stats = {
                "query": query,
                "category": category,
                "pages": 0,
                "inspected": 0,
                "candidates": 0,
                "known": 0,
                "new": 0,
                "rejected": 0,
                "resolution_failures": 0,
                "exhausted": False,
                "inspection_cap_reached": False,
                "duration_seconds": 0.0,
                "browser_resolutions": 0,
                "http_job_fetches": 0,
                "known_job_early_skips": 0,
            }
            page_start = 0
            query_inspection_cap = (
                min(10, max(1, limit))
                if diagnose_results
                else inspection_cap(limit)
            )
            seen_page_signatures = set()
            seen_result_identities = set()
            if verbose:
                print(f"[+] Searching jobs: {query}")

            while query_stats["inspected"] < query_inspection_cap and (
                diagnose_results or query_stats["new"] < limit
            ):
                page_limit = min(
                    10, query_inspection_cap - query_stats["inspected"]
                )
                try:
                    rows, marker = search_query(
                        driver, query, page_limit, timeout,
                        verbose=verbose, start=page_start,
                        diagnose_results=diagnose_results,
                        resolution_cache=resolution_cache,
                        recent_days=recent_days,
                    )
                    query_stats["pages"] += 1
                    stats["pages"] += 1
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
                    query_stats["exhausted"] = True
                    break

                page_identities = tuple(sorted({
                    (row.get("url") or row.get("raw_url") or "").strip()
                    for row in rows
                    if (row.get("url") or row.get("raw_url") or "").strip()
                }))
                if (
                    page_identities in seen_page_signatures
                    or not set(page_identities) - seen_result_identities
                ):
                    query_stats["exhausted"] = True
                    if verbose:
                        print("[+] Query exhausted: repeated result page")
                    break
                seen_page_signatures.add(page_identities)
                seen_result_identities.update(page_identities)

                for row in rows:
                    if query_stats["inspected"] >= query_inspection_cap or (
                        not diagnose_results and query_stats["new"] >= limit
                    ):
                        break
                    query_stats["inspected"] += 1
                    stats["inspected"] += 1
                    if row.get("click_resolution"):
                        query_stats["browser_resolutions"] += 1
                        stats["browser_resolutions"] += 1
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
                            query_stats["inspected"], row, classification
                        )
                    if resolution_error:
                        query_stats["resolution_failures"] += 1
                        stats["resolution_failures"] += 1
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
                            stats, classification, title, raw_url, verbose=verbose,
                            query_stats=query_stats,
                        )
                        break
                    if not classification.accepted:
                        record_rejection(
                            stats, classification, title, raw_url, verbose=verbose,
                            query_stats=query_stats,
                        )
                        continue
                    stats["candidates"] += 1
                    query_stats["candidates"] += 1
                    if diagnose_results:
                        continue
                    canonical = classification.normalized_url
                    provider = classification.provider
                    identity, source_job_id = _job_identity(provider, canonical)
                    known = None
                    if identity in run_jobs:
                        known = {"job_id": run_jobs[identity]}
                    else:
                        known = find_existing_job(
                            connection, provider, canonical, source_job_id
                        )
                    if known:
                        record_job_rediscovery(
                            connection, known["job_id"], provider, source_job_id,
                            canonical, query, source_evidence=_source_evidence(row),
                            query_category=category,
                        )
                        run_jobs[identity] = known["job_id"]
                        query_stats["known"] += 1
                        query_stats["known_job_early_skips"] += 1
                        stats["known"] += 1
                        stats["known_job_early_skips"] += 1
                        stats["job_states"].setdefault(known["job_id"], "KNOWN")
                        continue
                    query_stats["http_job_fetches"] += 1
                    stats["http_job_fetches"] += 1
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
                            query_stats=query_stats,
                        )
                        continue
                    job_id, was_new = upsert_job(
                        connection, parsed, query,
                        source_evidence=_source_evidence(row),
                        query_category=category,
                    )
                    run_jobs[identity] = job_id
                    if was_new:
                        query_stats["new"] += 1
                        stats["new"] += 1
                        stats["providers"][provider] += 1
                        stats["source_qualities"][
                            classify_source_quality(canonical, provider)
                        ] += 1
                        stats["job_states"][job_id] = "NEW"
                    else:
                        query_stats["known"] += 1
                        stats["known"] += 1
                        stats["job_states"].setdefault(job_id, "KNOWN")

                if diagnose_results or query_stats["new"] >= limit:
                    break
                if stats["stopped"]:
                    break
                if not has_next_search_page(driver):
                    query_stats["exhausted"] = True
                    break
                page_start += 10

            if (
                not diagnose_results
                and query_stats["inspected"] >= query_inspection_cap
                and query_stats["new"] < limit
            ):
                query_stats["inspection_cap_reached"] = True
                print(
                    f"[!] Inspection cap reached for query after "
                    f"{query_inspection_cap} results: {query}"
                )
            query_stats["duration_seconds"] = round(monotonic() - started, 3)
            stats["query_stats"].append(query_stats)
            if stats["stopped"]:
                break
            if verbose:
                print(
                    f"[+] Query stored {query_stats['new']} new jobs after "
                    f"inspecting {query_stats['inspected']} results"
                )
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
    for item in stats["query_stats"]:
        print(f"Query: {item['query']}")
        print(f"Category: {item['category']}")
        print(f"Pages inspected: {item['pages']}")
        print(f"Search results inspected: {item['inspected']}")
        print(f"Job candidates: {item['candidates']}")
        print(f"Known jobs: {item['known']}")
        print(f"New jobs: {item['new']}")
        print(f"Rejected/noise: {item['rejected']}")
        print(f"Resolution failures: {item['resolution_failures']}")
        print(f"Exhausted: {'yes' if item['exhausted'] else 'no'}")
        print(
            "Inspection cap reached: "
            f"{'yes' if item['inspection_cap_reached'] else 'no'}"
        )
        print(f"Duration seconds: {item['duration_seconds']:.3f}")
        print(f"Browser resolutions: {item['browser_resolutions']}")
        print(f"HTTP job fetches: {item['http_job_fetches']}")
        print(f"Known-job early skips: {item['known_job_early_skips']}")
    print(f"Queries processed: {stats['queries']}")
    print(f"Pages inspected: {stats['pages']}")
    print(f"Search results inspected: {stats['inspected']}")
    print(f"Job candidates: {stats['candidates']}")
    print(f"Known jobs: {stats['known']}")
    print(f"New jobs: {stats['new']}")
    print(f"Rejected/noise: {stats['rejected']}")
    print(f"Resolution failures: {stats['resolution_failures']}")
    print(f"Browser resolutions: {stats['browser_resolutions']}")
    print(f"HTTP job fetches: {stats['http_job_fetches']}")
    print(f"Known-job early skips: {stats['known_job_early_skips']}")
    print("By query category:")
    for category in (
        "ATS_SCOPED", "DIRECT_CAREERS", "ROLE_LOCATION", "ROLE_TECH", "GENERIC",
    ):
        print(f"{category}: {stats['query_categories'][category]}")
    print("Rejection reasons:")
    for reason, count in sorted(stats["rejection_reasons"].items()):
        print(f"  {reason}: {count}")
    print("By provider (new jobs):")
    for provider in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "teamtailor", "generic"):
        print(f"{provider.title()}: {stats['providers'][provider]}")
    print("By source quality (new jobs):")
    for quality in ("DIRECT_COMPANY", "ATS", "JOB_PLATFORM", "UNKNOWN"):
        print(f"{quality}: {stats['source_qualities'][quality]}")
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
    parser.add_argument(
        "--recent-days", type=int,
        help=(
            "Ask Google for recent results using tbs=qdr:dN; this is a "
            "best-effort Search filter and does not set published_at"
        ),
    )
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
    if arguments.recent_days is not None and arguments.recent_days < 1:
        parser.error("--recent-days must be at least 1")
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
        recent_days=arguments.recent_days,
    )
    print_summary(stats, arguments.database, arguments.diagnose_results)


if __name__ == "__main__":
    main()
