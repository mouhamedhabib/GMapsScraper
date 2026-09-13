"""Bounded maintenance for incomplete generic job rows.

Rows and identities are never deleted.  The command only fills missing job fields
and refreshes fetch diagnostics; listing-page exclusion is persisted separately by
``job_search.filtering --rebuild``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from job_search.providers import fetch_job, generic_listing_reason, parse_job_html
from job_search.storage import DEFAULT_DATABASE, connect_database, utc_now


def _company_id(connection, parsed, now):
    if not parsed.company_name:
        return None
    row = connection.execute(
        "SELECT company_id FROM companies WHERE canonical_name=? COLLATE NOCASE ORDER BY company_id LIMIT 1",
        (parsed.company_name,),
    ).fetchone()
    if row:
        return row["company_id"]
    cursor = connection.execute(
        """INSERT INTO companies
           (canonical_name, website_url, first_seen_at, last_seen_at, created_at, updated_at)
           VALUES (?, NULLIF(?, ''), ?, ?, ?, ?)""",
        (parsed.company_name, parsed.company_url, now, now, now, now),
    )
    return cursor.lastrowid


def _browser_fetch(url, timeout, driver):
    try:
        from selenium.webdriver.support.ui import WebDriverWait

        driver.set_page_load_timeout(timeout)
        driver.get(url)
        WebDriverWait(driver, timeout).until(
            lambda current: (
                current.page_source
                and "cf-chl-" not in current.page_source
                and "Just a moment..." not in current.page_source
            )
        )
        html = driver.page_source or ""
        if not html or "cf-chl-" in html or "Just a moment..." in html:
            return None
        return parse_job_html(url, html, "generic")
    except Exception:
        return None


def repair_incomplete_generic_jobs(
    connection, limit=None, timeout=15, browser_fallback=False,
    windowed=False, verbose=False, fetcher=fetch_job, driver_factory=None,
    job_ids=None,
):
    """Refetch incomplete generic postings and fill only currently missing fields."""
    query = """SELECT j.*, s.job_source_id, s.provider, s.fetch_status
               FROM jobs j JOIN job_sources s ON s.job_id=j.job_id
               WHERE s.provider='generic'
                 AND (j.title IS NULL OR j.description IS NULL
                      OR j.location_text IS NULL OR s.fetch_status='FAILED')"""
    parameters = []
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        query += f" AND j.job_id IN ({placeholders})"
        parameters.extend(job_ids)
    query += " ORDER BY j.job_id"
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    rows = connection.execute(query, parameters).fetchall()
    summary = {"examined": len(rows), "repaired": 0, "failed": 0, "listings": 0}
    driver = None
    now = utc_now()
    try:
        for row in rows:
            if generic_listing_reason(
                row["title"], row["canonical_url"], row["description"],
                page_fetched=True,
                has_structured_job_posting=row["status"] == "OPEN",
            ):
                summary["listings"] += 1
                if verbose:
                    print(f'{row["job_id"]}: listing page; left unchanged')
                continue
            parsed = fetcher(row["canonical_url"], timeout=timeout)
            if parsed.fetch_status == "FAILED" and browser_fallback:
                if driver is None:
                    if driver_factory is None:
                        from utils.google_search_discovery import create_chrome_driver
                        driver_factory = create_chrome_driver
                    driver = driver_factory(windowed=windowed)
                browser_result = _browser_fetch(row["canonical_url"], timeout, driver)
                if browser_result is not None:
                    parsed = browser_result

            useful = any((parsed.title, parsed.description, parsed.location_text, parsed.company_name))
            with connection:
                company_id = _company_id(connection, parsed, now) if useful and row["company_id"] is None else None
                connection.execute(
                    """UPDATE jobs SET
                         company_id=COALESCE(company_id, ?),
                         title=COALESCE(title, NULLIF(?, '')),
                         location_text=COALESCE(location_text, NULLIF(?, '')),
                         country=COALESCE(country, NULLIF(?, '')),
                         city=COALESCE(city, NULLIF(?, '')),
                         remote_policy=COALESCE(remote_policy, NULLIF(?, '')),
                         employment_type=COALESCE(employment_type, NULLIF(?, '')),
                         description=COALESCE(description, NULLIF(?, '')),
                         published_at=COALESCE(published_at, NULLIF(?, '')),
                         status=CASE WHEN ?='OPEN' THEN 'OPEN' ELSE status END,
                         content_hash=COALESCE(NULLIF(?, ''), content_hash),
                         updated_at=?
                       WHERE job_id=?""",
                    (company_id, parsed.title, parsed.location_text, parsed.country,
                     parsed.city, parsed.remote_policy, parsed.employment_type,
                     parsed.description, parsed.published_at, parsed.status,
                     parsed.content_hash, now, row["job_id"]),
                )
                connection.execute(
                    """UPDATE job_sources SET
                         apply_url=COALESCE(NULLIF(?, ''), apply_url),
                         last_fetched_at=?, fetch_status=?, fetch_error=NULLIF(?, ''),
                         raw_content_hash=COALESCE(NULLIF(?, ''), raw_content_hash)
                       WHERE job_source_id=?""",
                    (parsed.apply_url, now, parsed.fetch_status, parsed.fetch_error,
                     parsed.raw_content_hash, row["job_source_id"]),
                )
            if useful:
                summary["repaired"] += 1
                if verbose:
                    print(f'{row["job_id"]}: repaired {parsed.title or "(title still missing)"}')
            else:
                summary["failed"] += 1
                if verbose:
                    print(f'{row["job_id"]}: no useful content ({parsed.fetch_error or parsed.fetch_status})')
    finally:
        if driver is not None:
            driver.quit()
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description="Repair incomplete stored generic jobs")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--job-id", type=int, action="append", dest="job_ids", help="Repair only this job ID; repeatable")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--browser-fallback", action="store_true", help="Use one browser only after an HTTP fetch fails")
    parser.add_argument("--windowed", action="store_true", help="Show the fallback browser (requires --browser-fallback)")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.windowed and not args.browser_fallback:
        raise SystemExit("--windowed requires --browser-fallback")
    connection = connect_database(args.database)
    try:
        summary = repair_incomplete_generic_jobs(
            connection, args.limit, args.timeout, args.browser_fallback,
            args.windowed, args.verbose, job_ids=args.job_ids,
        )
    finally:
        connection.close()
    print(f"Generic rows examined: {summary['examined']}")
    print(f"Repaired: {summary['repaired']}")
    print(f"Still incomplete: {summary['failed']}")
    print(f"Listing pages left for filter exclusion: {summary['listings']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
