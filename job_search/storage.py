"""SQLite initialization and incremental job upserts."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from job_search.schema import (
    MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5, MIGRATION_6,
    MIGRATION_7, MIGRATION_8, MIGRATION_9,
    SCHEMA_VERSION,
)
from job_search.providers import classify_source_context


DEFAULT_DATABASE = Path("data/job_search.db")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_database(path=DEFAULT_DATABASE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize_schema(connection)
    return connection


def initialize_schema(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )
    migrations = (
        (1, MIGRATION_1), (2, MIGRATION_2), (3, MIGRATION_3), (4, MIGRATION_4),
        (5, MIGRATION_5), (6, MIGRATION_6), (7, MIGRATION_7),
        (8, MIGRATION_8), (9, MIGRATION_9),
    )
    for migration_version, script in migrations:
        if version < migration_version:
            connection.executescript(script)
            connection.execute(f"PRAGMA user_version = {migration_version}")
            connection.commit()
            version = migration_version
    _backfill_source_queries(connection)


def normalize_source_query(value):
    """Return the case-insensitive whitespace-normalized provenance key."""
    return " ".join(str(value or "").split()).casefold()


def _upsert_source_query(
    connection, job_id, job_source_id, source_query, now,
    source_evidence=None, query_category="",
):
    original = str(source_query or "").strip()
    normalized = normalize_source_query(original)
    if not normalized:
        return
    evidence = source_evidence or {}
    connection.execute(
        """INSERT INTO job_source_queries
           (job_id, job_source_id, source_query, normalized_query, query_category,
            result_snippet, displayed_domain, google_result_date_text,
            first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                   NULLIF(?, ''), ?, ?)
           ON CONFLICT(job_source_id, normalized_query) DO UPDATE SET
             query_category=COALESCE(NULLIF(excluded.query_category, ''), query_category),
             result_snippet=COALESCE(NULLIF(excluded.result_snippet, ''), result_snippet),
             displayed_domain=COALESCE(NULLIF(excluded.displayed_domain, ''), displayed_domain),
             google_result_date_text=COALESCE(
                 NULLIF(excluded.google_result_date_text, ''), google_result_date_text
             ),
             last_seen_at=excluded.last_seen_at""",
        (
            job_id, job_source_id, original, normalized, query_category,
            evidence.get("result_snippet", ""),
            evidence.get("displayed_domain", ""),
            evidence.get("google_result_date_text", ""), now, now,
        ),
    )


def _backfill_source_queries(connection):
    """Normalize legacy semicolon provenance without discarding its text."""
    if connection.execute("PRAGMA user_version").fetchone()[0] < 6:
        return
    sources = connection.execute(
        """SELECT job_source_id, job_id, source_query, first_seen_at, last_seen_at
           FROM job_sources WHERE source_query IS NOT NULL AND trim(source_query) != ''"""
    ).fetchall()
    with connection:
        for source in sources:
            for query in source["source_query"].split(";"):
                original = query.strip()
                normalized = normalize_source_query(original)
                if not normalized:
                    continue
                connection.execute(
                    """INSERT OR IGNORE INTO job_source_queries
                       (job_id, job_source_id, source_query, normalized_query,
                        first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source["job_id"], source["job_source_id"], original,
                        normalized, source["first_seen_at"], source["last_seen_at"],
                    ),
                )


def _domain(url):
    return (urlsplit(url).hostname or "").casefold().removeprefix("www.")


def _company_id(connection, job, now):
    name = (job.company_name or "").strip()
    website = (job.company_url or "").strip()
    domain = _domain(website)
    if not name and not domain:
        return None
    row = None
    if domain:
        row = connection.execute(
            "SELECT company_id FROM companies WHERE normalized_domain = ?", (domain,)
        ).fetchone()
    elif name:
        row = connection.execute(
            "SELECT company_id FROM companies WHERE canonical_name = ? COLLATE NOCASE ORDER BY company_id LIMIT 1",
            (name,),
        ).fetchone()
    if row:
        connection.execute(
            """UPDATE companies SET
                   canonical_name = COALESCE(NULLIF(?, ''), canonical_name),
                   normalized_domain = COALESCE(NULLIF(?, ''), normalized_domain),
                   website_url = COALESCE(NULLIF(?, ''), website_url),
                   last_seen_at = ?, updated_at = ?
               WHERE company_id = ?""",
            (name, domain, website, now, now, row["company_id"]),
        )
        return row["company_id"]
    cursor = connection.execute(
        """INSERT INTO companies
               (canonical_name, normalized_domain, website_url, first_seen_at,
                last_seen_at, created_at, updated_at)
           VALUES (?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, ?, ?)""",
        (name or None, domain, website, now, now, now, now),
    )
    return cursor.lastrowid


def _merge_queries(existing, incoming):
    values = []
    seen = set()
    for value in (existing or "", incoming or ""):
        for item in value.split(";"):
            cleaned = item.strip()
            if cleaned and cleaned.casefold() not in seen:
                seen.add(cleaned.casefold())
                values.append(cleaned)
    return ";".join(values)


def find_existing_job(connection, provider, canonical_url, source_job_id=""):
    """Resolve a stored job before fetching its page."""
    if source_job_id:
        row = connection.execute(
            """SELECT j.job_id, s.job_source_id
               FROM jobs j JOIN job_sources s ON s.job_id=j.job_id
               WHERE s.provider=? AND s.source_job_id=? LIMIT 1""",
            (provider, source_job_id),
        ).fetchone()
        if row:
            return row
    return connection.execute(
        """SELECT j.job_id, s.job_source_id
           FROM jobs j LEFT JOIN job_sources s
             ON s.job_id=j.job_id AND s.provider=?
           WHERE j.canonical_url=?
           ORDER BY s.job_source_id LIMIT 1""",
        (provider, canonical_url),
    ).fetchone()


def record_job_rediscovery(
    connection, job_id, provider, source_job_id, source_url, source_query,
    now=None, source_evidence=None, query_category="",
):
    """Update seen/provenance state for a known job without fetching it."""
    now = now or utc_now()
    with connection:
        source = None
        if source_job_id:
            source = connection.execute(
                """SELECT * FROM job_sources
                   WHERE provider=? AND source_job_id=? LIMIT 1""",
                (provider, source_job_id),
            ).fetchone()
        if source is None:
            source = connection.execute(
                """SELECT * FROM job_sources
                   WHERE job_id=? AND provider=? AND source_url=? LIMIT 1""",
                (job_id, provider, source_url),
            ).fetchone()
        connection.execute(
            "UPDATE jobs SET last_seen_at=?, updated_at=? WHERE job_id=?",
            (now, now, job_id),
        )
        if source:
            queries = _merge_queries(source["source_query"], source_query)
            connection.execute(
                """UPDATE job_sources SET source_query=?, last_seen_at=?
                   WHERE job_source_id=?""",
                (queries, now, source["job_source_id"]),
            )
            job_source_id = source["job_source_id"]
        else:
            cursor = connection.execute(
                """INSERT INTO job_sources
                   (job_id, provider, source_job_id, source_url, source_query,
                    first_seen_at, last_seen_at, fetch_status)
                   VALUES (?, ?, NULLIF(?, ''), ?, NULLIF(?, ''), ?, ?, 'NOT_FETCHED')""",
                (job_id, provider, source_job_id, source_url, source_query, now, now),
            )
            job_source_id = cursor.lastrowid
        _upsert_source_query(
            connection, job_id, job_source_id, source_query, now,
            source_evidence, query_category,
        )


def upsert_job(
    connection, job, source_query, now=None, source_evidence=None,
    query_category="",
):
    """Persist one discovery and return ``(job_id, was_new)`` atomically."""
    now = now or utc_now()
    source_context = classify_source_context(
        job.canonical_url, job.provider, job.company_name,
    )
    with connection:
        company_id = _company_id(connection, job, now)
        existing = None
        if job.source_job_id:
            existing = connection.execute(
                """SELECT j.job_id, j.first_seen_at
                   FROM jobs j JOIN job_sources s ON s.job_id = j.job_id
                   WHERE s.provider = ? AND s.source_job_id = ?""",
                (job.provider, job.source_job_id),
            ).fetchone()
        if existing is None:
            existing = connection.execute(
                "SELECT job_id, first_seen_at FROM jobs WHERE canonical_url = ?",
                (job.canonical_url,),
            ).fetchone()

        was_new = existing is None
        if was_new:
            cursor = connection.execute(
                """INSERT INTO jobs
                   (company_id, canonical_url, title, location_text, country, city, region,
                    remote_policy, employment_type, seniority, description,
                    published_at, first_seen_at, last_seen_at, status, content_hash,
                    created_at, updated_at)
                   VALUES (?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                           NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                           NULLIF(?, ''),
                           NULLIF(?, ''), NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?)""",
                (
                    company_id, job.canonical_url, job.title, job.location_text,
                    job.country, job.city, job.region, job.remote_policy, job.employment_type,
                    job.seniority, job.description, job.published_at, now, now,
                    job.status, job.content_hash, now, now,
                ),
            )
            job_id = cursor.lastrowid
        else:
            job_id = existing["job_id"]
            connection.execute(
                """UPDATE jobs SET
                   company_id = COALESCE(?, company_id),
                   title = COALESCE(NULLIF(?, ''), title),
                   location_text = COALESCE(NULLIF(?, ''), location_text),
                   country = COALESCE(NULLIF(?, ''), country),
                   city = COALESCE(NULLIF(?, ''), city),
                   region = COALESCE(NULLIF(?, ''), region),
                   remote_policy = COALESCE(NULLIF(?, ''), remote_policy),
                   employment_type = COALESCE(NULLIF(?, ''), employment_type),
                   seniority = COALESCE(NULLIF(?, ''), seniority),
                   description = COALESCE(NULLIF(?, ''), description),
                   published_at = COALESCE(NULLIF(?, ''), published_at),
                   last_seen_at = ?,
                   status = CASE WHEN ? = 'OPEN' THEN 'OPEN' ELSE status END,
                   content_hash = COALESCE(NULLIF(?, ''), content_hash),
                   updated_at = ?
                   WHERE job_id = ?""",
                (
                    company_id, job.title, job.location_text, job.country, job.city,
                    job.region,
                    job.remote_policy, job.employment_type, job.seniority,
                    job.description, job.published_at, now, job.status,
                    job.content_hash, now, job_id,
                ),
            )

        source = None
        if job.source_job_id:
            source = connection.execute(
                "SELECT * FROM job_sources WHERE provider = ? AND source_job_id = ?",
                (job.provider, job.source_job_id),
            ).fetchone()
        if source is None:
            source = connection.execute(
                "SELECT * FROM job_sources WHERE provider = ? AND source_url = ?",
                (job.provider, job.canonical_url),
            ).fetchone()
        if source:
            queries = _merge_queries(source["source_query"], source_query)
            connection.execute(
                """UPDATE job_sources SET job_id = ?, source_url = ?,
                   apply_url = COALESCE(NULLIF(?, ''), apply_url), source_query = ?,
                   last_seen_at = ?, last_fetched_at = ?, fetch_status = ?,
                   fetch_error = NULLIF(?, ''), raw_content_hash = COALESCE(NULLIF(?, ''), raw_content_hash),
                   source_type=?, employer_relationship=?
                   WHERE job_source_id = ?""",
                (
                    job_id, job.canonical_url, job.apply_url, queries, now, now,
                    job.fetch_status, job.fetch_error, job.raw_content_hash,
                    source_context.source_type, source_context.employer_relationship,
                    source["job_source_id"],
                ),
            )
            job_source_id = source["job_source_id"]
        else:
            cursor = connection.execute(
                """INSERT INTO job_sources
                   (job_id, provider, source_job_id, source_url, apply_url,
                    source_query, first_seen_at, last_seen_at, last_fetched_at,
                    fetch_status, fetch_error, raw_content_hash, source_type,
                    employer_relationship)
                   VALUES (?, ?, NULLIF(?, ''), ?, NULLIF(?, ''), NULLIF(?, ''),
                           ?, ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?)""",
                (
                    job_id, job.provider, job.source_job_id, job.canonical_url,
                    job.apply_url, source_query, now, now, now, job.fetch_status,
                    job.fetch_error, job.raw_content_hash, source_context.source_type,
                    source_context.employer_relationship,
                ),
            )
            job_source_id = cursor.lastrowid
        _upsert_source_query(
            connection, job_id, job_source_id, source_query, now,
            source_evidence, query_category,
        )
    return job_id, was_new
