"""Versioned SQLite schema for the job-search layer."""

SCHEMA_VERSION = 23

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

MIGRATION_10 = """
ALTER TABLE workflow_runs ADD COLUMN network_pauses INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workflow_runs ADD COLUMN network_pause_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE workflow_runs ADD COLUMN network_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workflow_runs ADD COLUMN network_recoveries INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workflow_runs ADD COLUMN last_network_failure TEXT;
ALTER TABLE workflow_runs ADD COLUMN last_successful_probe TEXT;

CREATE TABLE IF NOT EXISTS workflow_run_queries (
    workflow_run_query_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    source_query TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'PLANNED', 'RUNNING', 'COMPLETED', 'EXHAUSTED',
        'BLOCKED_VERIFICATION', 'NETWORK_INTERRUPTED', 'FAILED',
        'FAILED_RETRYABLE'
    )),
    pages_inspected INTEGER NOT NULL DEFAULT 0,
    results_inspected INTEGER NOT NULL DEFAULT 0,
    new_jobs INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_message TEXT,
    network_failure_count INTEGER NOT NULL DEFAULT 0,
    recovered_network_failures INTEGER NOT NULL DEFAULT 0,
    page_start_offset INTEGER NOT NULL DEFAULT 0,
    network_pause_count INTEGER NOT NULL DEFAULT 0,
    last_network_failure TEXT,
    last_successful_probe TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, source_query)
);

CREATE INDEX IF NOT EXISTS idx_workflow_run_queries_status
    ON workflow_run_queries(run_id, status);
"""

MIGRATION_11 = """
-- Nullable analytics columns deliberately distinguish pre-v11 unavailable
-- evidence from a measured zero in newer workflow runs.
ALTER TABLE workflow_run_queries ADD COLUMN query_category TEXT;
ALTER TABLE workflow_run_queries ADD COLUMN job_candidates INTEGER;
ALTER TABLE workflow_run_queries ADD COLUMN known_jobs INTEGER;
ALTER TABLE workflow_run_queries ADD COLUMN rejected_noise INTEGER;
ALTER TABLE workflow_run_queries ADD COLUMN resolution_failures INTEGER;
ALTER TABLE workflow_run_queries ADD COLUMN browser_resolutions INTEGER;
ALTER TABLE workflow_run_queries ADD COLUMN http_job_fetches INTEGER;
ALTER TABLE workflow_run_queries ADD COLUMN duration_seconds REAL;

CREATE TABLE workflow_run_job_queries (
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    source_query TEXT NOT NULL,
    discovery_state TEXT NOT NULL CHECK (discovery_state IN ('NEW', 'KNOWN')),
    is_primary_new_source INTEGER NOT NULL DEFAULT 0
        CHECK (is_primary_new_source IN (0, 1)),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id, source_query),
    FOREIGN KEY (run_id, source_query)
        REFERENCES workflow_run_queries(run_id, source_query) ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_workflow_run_job_queries_primary_new
    ON workflow_run_job_queries(run_id, job_id)
    WHERE is_primary_new_source = 1;
CREATE INDEX idx_workflow_run_job_queries_query
    ON workflow_run_job_queries(run_id, source_query);
"""

MIGRATION_12 = """
CREATE TABLE job_qualifications (
    qualification_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    qualification_status TEXT NOT NULL
        CHECK (qualification_status IN ('QUALIFIED', 'REVIEW', 'DISQUALIFIED')),
    activity_status TEXT NOT NULL
        CHECK (activity_status IN ('ACTIVE', 'INACTIVE', 'UNKNOWN')),
    employer_status TEXT NOT NULL
        CHECK (employer_status IN ('CONFIRMED', 'UNKNOWN')),
    application_channel TEXT NOT NULL CHECK (application_channel IN (
        'DIRECT_COMPANY', 'ATS', 'RECRUITER', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    application_url TEXT,
    reason_codes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    input_evidence_hash TEXT NOT NULL,
    qualified_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_qualifications_status
    ON job_qualifications(policy_version, qualification_status);
"""

MIGRATION_13 = """
CREATE TABLE job_location_eligibility (
    location_evidence_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    work_model TEXT NOT NULL
        CHECK (work_model IN ('ONSITE', 'HYBRID', 'REMOTE', 'UNKNOWN')),
    remote_scope TEXT NOT NULL CHECK (remote_scope IN (
        'WORLDWIDE', 'EUROPE', 'EU', 'EEA', 'COUNTRY', 'REGION', 'CITY', 'UNKNOWN'
    )),
    required_country TEXT,
    required_region TEXT,
    required_city TEXT,
    residency_requirement TEXT NOT NULL
        CHECK (residency_requirement IN ('REQUIRED', 'NOT_STATED', 'UNKNOWN')),
    work_authorization_requirement TEXT NOT NULL
        CHECK (work_authorization_requirement IN ('REQUIRED', 'NOT_STATED', 'UNKNOWN')),
    work_authorization_jurisdiction TEXT,
    visa_sponsorship TEXT NOT NULL
        CHECK (visa_sponsorship IN ('AVAILABLE', 'NOT_AVAILABLE', 'NOT_STATED', 'UNKNOWN')),
    relocation_support TEXT NOT NULL
        CHECK (relocation_support IN ('AVAILABLE', 'NOT_AVAILABLE', 'NOT_STATED', 'UNKNOWN')),
    location_eligibility_status TEXT NOT NULL
        CHECK (location_eligibility_status IN ('KNOWN', 'PARTIAL', 'UNKNOWN')),
    evidence_json TEXT NOT NULL,
    input_evidence_hash TEXT NOT NULL,
    fetch_status TEXT NOT NULL,
    fetch_error TEXT,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_location_eligibility_status
    ON job_location_eligibility(policy_version, location_eligibility_status);
"""

MIGRATION_14 = """
CREATE TABLE job_activity_evidence (
    activity_evidence_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    activity_status TEXT NOT NULL
        CHECK (activity_status IN ('ACTIVE', 'INACTIVE', 'UNKNOWN')),
    authoritative_url TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'DIRECT_COMPANY', 'ATS', 'RECRUITER', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    provider TEXT NOT NULL,
    http_status INTEGER,
    evidence_method TEXT NOT NULL,
    raw_evidence_summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    fetch_status TEXT NOT NULL,
    fetch_error TEXT,
    input_fingerprint TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_activity_evidence_status
    ON job_activity_evidence(policy_version, activity_status);
"""

MIGRATION_15 = """
ALTER TABLE job_qualifications RENAME TO job_qualifications_v14;

CREATE TABLE job_qualifications (
    qualification_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    qualification_status TEXT NOT NULL
        CHECK (qualification_status IN ('QUALIFIED', 'REVIEW', 'DISQUALIFIED')),
    activity_status TEXT NOT NULL
        CHECK (activity_status IN ('ACTIVE', 'INACTIVE', 'UNKNOWN')),
    employer_status TEXT NOT NULL
        CHECK (employer_status IN ('CONFIRMED', 'UNKNOWN', 'CONFLICT')),
    application_channel TEXT NOT NULL CHECK (application_channel IN (
        'DIRECT_COMPANY', 'ATS', 'RECRUITER', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    application_url TEXT,
    reason_codes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    input_evidence_hash TEXT NOT NULL,
    qualified_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

INSERT INTO job_qualifications
SELECT * FROM job_qualifications_v14;
DROP TABLE job_qualifications_v14;
CREATE INDEX idx_job_qualifications_status
    ON job_qualifications(policy_version, qualification_status);

CREATE TABLE job_employer_evidence (
    employer_evidence_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    employer_status TEXT NOT NULL
        CHECK (employer_status IN ('CONFIRMED', 'UNKNOWN', 'CONFLICT')),
    actual_employer TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'DIRECT_COMPANY', 'ATS', 'RECRUITER', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    employer_relationship TEXT NOT NULL CHECK (employer_relationship IN (
        'DIRECT', 'RECRUITER', 'AGGREGATOR', 'UNKNOWN'
    )),
    provider TEXT NOT NULL,
    authoritative_url TEXT,
    evidence_method TEXT NOT NULL,
    raw_evidence_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    fetch_result TEXT NOT NULL,
    fetch_error TEXT,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_employer_evidence_status
    ON job_employer_evidence(policy_version, employer_status);
"""

MIGRATION_16 = """
CREATE TABLE job_application_destinations (
    application_destination_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    application_status TEXT NOT NULL CHECK (application_status IN (
        'CONFIRMED', 'UNKNOWN', 'CONFLICT', 'UNAVAILABLE'
    )),
    application_channel TEXT NOT NULL CHECK (application_channel IN (
        'DIRECT_COMPANY', 'ATS', 'RECRUITER', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    application_url TEXT,
    provider TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'ATS', 'COMPANY_SITE', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    employer_relationship TEXT NOT NULL CHECK (employer_relationship IN (
        'DIRECT', 'RECRUITER', 'AGGREGATOR', 'UNKNOWN'
    )),
    authoritative_source_url TEXT,
    http_status INTEGER,
    evidence_method TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    fetch_result TEXT NOT NULL,
    fetch_error TEXT,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_application_destinations_status
    ON job_application_destinations(policy_version, application_status);
"""

MIGRATION_17 = """
CREATE TABLE job_contact_strategies (
    contact_strategy_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    primary_role TEXT NOT NULL,
    secondary_roles_json TEXT NOT NULL,
    avoid_roles_json TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH', 'MEDIUM', 'LOW')),
    rationale TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_contact_strategies_confidence
    ON job_contact_strategies(policy_version, confidence);
"""

MIGRATION_18 = """
CREATE TABLE job_contact_candidates (
    contact_candidate_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    person_key TEXT NOT NULL,
    person_name TEXT NOT NULL,
    current_title TEXT NOT NULL,
    company TEXT NOT NULL,
    target_role_category TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'JOB_POSTING', 'OFFICIAL_COMPANY', 'LINKEDIN', 'GITHUB', 'OTHER_PROFESSIONAL'
    )),
    relationship_to_job TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH', 'MEDIUM', 'LOW')),
    selection_status TEXT NOT NULL CHECK (selection_status IN (
        'SELECTED_PRIMARY', 'SELECTED_BACKUP', 'REJECTED_ROLE_MISMATCH',
        'REJECTED_COMPANY_MISMATCH', 'REJECTED_FORMER_EMPLOYEE',
        'REJECTED_LOW_CONFIDENCE'
    )),
    reason_codes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    discovery_query TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version, person_key)
);

CREATE INDEX idx_job_contact_candidates_status
    ON job_contact_candidates(job_id, policy_version, selection_status);

CREATE TABLE job_selected_contacts (
    selected_contact_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    discovery_status TEXT NOT NULL CHECK (discovery_status IN (
        'CONTACTS_SELECTED', 'NO_CONFIDENT_CONTACT'
    )),
    primary_contact_candidate_id INTEGER REFERENCES job_contact_candidates(contact_candidate_id),
    backup_contact_candidate_id INTEGER REFERENCES job_contact_candidates(contact_candidate_id),
    search_queries_used INTEGER NOT NULL,
    people_inspected INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);
"""

MIGRATION_19 = """
ALTER TABLE job_selected_contacts
    ADD COLUMN first_party_pages_inspected INTEGER NOT NULL DEFAULT 0;
"""

MIGRATION_20 = """
CREATE TABLE job_company_websites (
    company_website_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'UNKNOWN', 'CONFLICT', 'UNAVAILABLE')),
    company_name TEXT NOT NULL,
    website_url TEXT,
    canonical_domain TEXT,
    source_url TEXT,
    original_candidate_url TEXT,
    final_url TEXT,
    redirect_chain_json TEXT NOT NULL,
    evidence_method TEXT NOT NULL,
    source_type TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    http_status INTEGER,
    fetch_result TEXT NOT NULL,
    fetch_error TEXT,
    network_used INTEGER NOT NULL CHECK (network_used IN (0, 1)),
    input_fingerprint TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_company_websites_status
    ON job_company_websites(policy_version, status);
"""

MIGRATION_21 = """
CREATE TABLE job_contact_outcomes (
    contact_outcome_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'EMAIL_DISCOVERY_READY', 'APPLY_ONLY', 'CONTACT_REVIEW'
    )),
    primary_contact_candidate_id INTEGER,
    backup_contact_candidate_id INTEGER,
    application_status TEXT NOT NULL CHECK (application_status IN (
        'CONFIRMED', 'UNKNOWN', 'CONFLICT', 'UNAVAILABLE'
    )),
    application_channel TEXT NOT NULL CHECK (application_channel IN (
        'DIRECT_COMPANY', 'ATS', 'RECRUITER', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    reason_codes_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_contact_outcomes_outcome
    ON job_contact_outcomes(policy_version, outcome);
"""

MIGRATION_22 = """
CREATE TABLE job_application_actions (
    application_action_id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    policy_version TEXT NOT NULL,
    application_identity TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'READY_TO_APPLY', 'APPLYING', 'APPLIED', 'FAILED', 'SKIPPED',
        'WITHDRAWN', 'CLOSED_BEFORE_APPLY'
    )),
    duplicate_status TEXT NOT NULL CHECK (duplicate_status IN (
        'NO_DUPLICATE', 'ALREADY_APPLIED', 'POSSIBLE_DUPLICATE', 'IDENTITY_CONFLICT'
    )),
    application_channel TEXT NOT NULL CHECK (application_channel IN (
        'DIRECT_COMPANY', 'ATS', 'RECRUITER', 'JOB_PLATFORM', 'UNKNOWN'
    )),
    application_url TEXT NOT NULL,
    canonical_application_url TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_job_id TEXT,
    contact_outcome TEXT NOT NULL CHECK (contact_outcome IN (
        'EMAIL_DISCOVERY_READY', 'APPLY_ONLY', 'CONTACT_REVIEW'
    )),
    qualification_status TEXT NOT NULL CHECK (qualification_status IN (
        'QUALIFIED', 'REVIEW', 'DISQUALIFIED'
    )),
    reason_codes_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    ready_at TEXT,
    applied_at TEXT,
    withdrawn_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, policy_version)
);

CREATE INDEX idx_job_application_actions_identity
    ON job_application_actions(policy_version, application_identity);
CREATE INDEX idx_job_application_actions_state
    ON job_application_actions(policy_version, state, duplicate_status);
CREATE INDEX idx_job_application_actions_url
    ON job_application_actions(policy_version, canonical_application_url);
CREATE INDEX idx_job_application_actions_provider_job
    ON job_application_actions(policy_version, provider, provider_job_id);

CREATE TABLE job_application_events (
    application_event_id INTEGER PRIMARY KEY,
    application_action_id INTEGER NOT NULL
        REFERENCES job_application_actions(application_action_id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_job_application_events_action
    ON job_application_events(application_action_id, application_event_id);
"""

MIGRATION_23 = """
CREATE TABLE query_strategy_runs (
    strategy_run_id INTEGER PRIMARY KEY,
    policy_version TEXT NOT NULL,
    source_run_scope TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    config_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    UNIQUE (policy_version, input_fingerprint)
);

CREATE TABLE query_strategy_recommendations (
    strategy_recommendation_id INTEGER PRIMARY KEY,
    strategy_run_id INTEGER NOT NULL
        REFERENCES query_strategy_runs(strategy_run_id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    recommendation TEXT NOT NULL CHECK (recommendation IN (
        'KEEP', 'EXPAND', 'REVIEW', 'RETIRE_CANDIDATE'
    )),
    evidence_maturity TEXT NOT NULL CHECK (evidence_maturity IN (
        'INSUFFICIENT', 'EARLY', 'MATURE'
    )),
    reason_codes_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (strategy_run_id, query)
);

CREATE TABLE query_strategy_proposals (
    strategy_proposal_id INTEGER PRIMARY KEY,
    strategy_run_id INTEGER NOT NULL
        REFERENCES query_strategy_runs(strategy_run_id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    parent_query TEXT NOT NULL,
    proposal_rank INTEGER NOT NULL,
    reason_codes_json TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (strategy_run_id, query),
    UNIQUE (strategy_run_id, proposal_rank)
);

CREATE INDEX idx_query_strategy_recommendations_result
    ON query_strategy_recommendations(strategy_run_id, recommendation, evidence_maturity);
"""
