"""Versioned SQLite schema for the job-search layer."""

SCHEMA_VERSION = 2

MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS companies (
    company_id INTEGER PRIMARY KEY,
    canonical_name TEXT,
    normalized_domain TEXT UNIQUE,
    website_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(company_id),
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT,
    location_text TEXT,
    country TEXT,
    city TEXT,
    remote_policy TEXT,
    employment_type TEXT,
    seniority TEXT,
    description TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (status IN ('OPEN', 'UNKNOWN', 'REMOVED', 'CLOSED')),
    content_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_sources (
    job_source_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    source_job_id TEXT,
    source_url TEXT NOT NULL,
    apply_url TEXT,
    source_query TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_fetched_at TEXT,
    fetch_status TEXT,
    fetch_error TEXT,
    raw_content_hash TEXT,
    UNIQUE (provider, source_job_id),
    UNIQUE (provider, source_url)
);

CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_job_sources_job_id ON job_sources(job_id);
"""

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS job_filter_results (
    filter_result_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'REVIEW', 'REJECT')),
    primary_reason TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    matched_terms_json TEXT NOT NULL,
    experience_min_years INTEGER,
    experience_max_years INTEGER,
    detected_remote_policy TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (detected_remote_policy IN ('REMOTE', 'HYBRID', 'ONSITE', 'UNKNOWN')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX IF NOT EXISTS idx_job_filter_results_status
    ON job_filter_results(policy_version, status);
CREATE INDEX IF NOT EXISTS idx_job_filter_results_job_id
    ON job_filter_results(job_id);
"""
