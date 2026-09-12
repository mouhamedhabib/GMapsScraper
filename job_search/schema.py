"""Versioned SQLite schema for the job-search layer."""

SCHEMA_VERSION = 9

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

MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS job_repair_results (
    repair_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('REPAIRED', 'NO_CHANGE', 'FAILED', 'BLOCKED')),
    fields_filled_json TEXT NOT NULL DEFAULT '{}',
    source_type TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version, attempted_at)
);

CREATE INDEX IF NOT EXISTS idx_job_repair_results_job_id
    ON job_repair_results(job_id);
CREATE INDEX IF NOT EXISTS idx_job_repair_results_status
    ON job_repair_results(policy_version, status);
"""

MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS job_repair_cleanups (
    cleanup_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    cleaned_at TEXT NOT NULL,
    fields_cleared_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version, cleaned_at)
);

CREATE INDEX IF NOT EXISTS idx_job_repair_cleanups_job_id
    ON job_repair_cleanups(job_id);
"""

MIGRATION_5 = """
ALTER TABLE job_repair_results
    ADD COLUMN final_pass INTEGER NOT NULL DEFAULT 0 CHECK (final_pass IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_job_repair_results_final_pass
    ON job_repair_results(job_id, final_pass);
"""

MIGRATION_6 = """
CREATE TABLE IF NOT EXISTS job_source_queries (
    job_source_query_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    job_source_id INTEGER NOT NULL REFERENCES job_sources(job_source_id) ON DELETE CASCADE,
    source_query TEXT NOT NULL,
    normalized_query TEXT NOT NULL,
    query_category TEXT,
    result_snippet TEXT,
    displayed_domain TEXT,
    google_result_date_text TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (job_source_id, normalized_query)
);

CREATE INDEX IF NOT EXISTS idx_job_source_queries_job_id
    ON job_source_queries(job_id);
CREATE INDEX IF NOT EXISTS idx_job_source_queries_source_id
    ON job_source_queries(job_source_id);
"""

MIGRATION_7 = """
ALTER TABLE job_sources ADD COLUMN source_type TEXT
    CHECK (source_type IN ('ATS', 'COMPANY_SITE', 'JOB_PLATFORM', 'UNKNOWN'));
ALTER TABLE job_sources ADD COLUMN employer_relationship TEXT
    CHECK (employer_relationship IN ('DIRECT', 'RECRUITER', 'AGGREGATOR', 'UNKNOWN'));

ALTER TABLE job_repair_results ADD COLUMN completion_status TEXT
    CHECK (completion_status IN ('SUCCESS', 'PARTIAL', 'NO_CHANGE', 'BLOCKED', 'FAILED'));
ALTER TABLE job_repair_results ADD COLUMN field_changes_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE job_repair_results ADD COLUMN experience_evidence_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE job_repair_results ADD COLUMN employer_relationship TEXT
    CHECK (employer_relationship IN ('DIRECT', 'RECRUITER', 'AGGREGATOR', 'UNKNOWN'));
"""

MIGRATION_8 = """
ALTER TABLE jobs ADD COLUMN region TEXT;
"""

MIGRATION_9 = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'INTERRUPTED')),
    mode TEXT NOT NULL,
    maps_enabled INTEGER NOT NULL CHECK (maps_enabled IN (0, 1)),
    job_discovery_enabled INTEGER NOT NULL CHECK (job_discovery_enabled IN (0, 1)),
    completion_enabled INTEGER NOT NULL CHECK (completion_enabled IN (0, 1)),
    filter_enabled INTEGER NOT NULL CHECK (filter_enabled IN (0, 1)),
    priority_enabled INTEGER NOT NULL CHECK (priority_enabled IN (0, 1)),
    error_summary TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_run_jobs (
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    discovery_state TEXT NOT NULL
        CHECK (discovery_state IN ('NEW', 'KNOWN', 'UPDATED')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_run_jobs_state
    ON workflow_run_jobs(run_id, discovery_state);
"""
