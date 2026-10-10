"""Versioned SQLite schema for persistent company discovery identity."""

SCHEMA_VERSION = 6

MIGRATION_1 = """
CREATE TABLE companies (
    company_id TEXT PRIMARY KEY
        CHECK (length(trim(company_id)) > 0),
    canonical_name TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN'
        CHECK (discovery_status IN ('LEGACY_UNKNOWN', 'OBSERVED')),
    first_seen_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    CHECK (first_seen_at IS NULL OR length(trim(first_seen_at)) > 0),
    CHECK (last_seen_at IS NULL OR length(trim(last_seen_at)) > 0),
    CHECK (discovery_status = 'LEGACY_UNKNOWN' OR first_seen_at IS NOT NULL)
);

CREATE TABLE branches (
    branch_id TEXT PRIMARY KEY
        CHECK (length(trim(branch_id)) > 0),
    company_id TEXT NOT NULL
        REFERENCES companies(company_id) ON DELETE CASCADE,
    display_name TEXT,
    google_maps_place_id TEXT UNIQUE,
    website_url TEXT,
    phone TEXT,
    address TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN'
        CHECK (discovery_status IN ('LEGACY_UNKNOWN', 'OBSERVED')),
    first_seen_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    CHECK (google_maps_place_id IS NULL OR length(trim(google_maps_place_id)) > 0),
    CHECK (first_seen_at IS NULL OR length(trim(first_seen_at)) > 0),
    CHECK (last_seen_at IS NULL OR length(trim(last_seen_at)) > 0),
    CHECK (discovery_status = 'LEGACY_UNKNOWN' OR first_seen_at IS NOT NULL)
);

CREATE TABLE company_identities (
    identity_id TEXT PRIMARY KEY
        CHECK (length(trim(identity_id)) > 0),
    company_id TEXT NOT NULL
        REFERENCES companies(company_id) ON DELETE CASCADE,
    identity_type TEXT NOT NULL CHECK (identity_type IN (
        'NAME', 'WEBSITE_DOMAIN', 'PHONE', 'EMAIL_DOMAIN',
        'REGISTRATION_NUMBER', 'OTHER'
    )),
    identity_value TEXT NOT NULL CHECK (length(trim(identity_value)) > 0),
    normalized_value TEXT NOT NULL CHECK (length(trim(normalized_value)) > 0),
    first_seen_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    CHECK (first_seen_at IS NULL OR length(trim(first_seen_at)) > 0),
    CHECK (last_seen_at IS NULL OR length(trim(last_seen_at)) > 0),
    UNIQUE (company_id, identity_type, normalized_value)
);

CREATE TABLE discovery_runs (
    run_id TEXT PRIMARY KEY
        CHECK (length(trim(run_id)) > 0),
    run_type TEXT NOT NULL
        CHECK (run_type IN ('SCRAPE', 'LEGACY_IMPORT', 'MANUAL')),
    status TEXT NOT NULL CHECK (status IN (
        'RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'INTERRUPTED'
    )),
    started_at TEXT NOT NULL CHECK (length(trim(started_at)) > 0),
    finished_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    CHECK (finished_at IS NULL OR length(trim(finished_at)) > 0)
);

CREATE TABLE run_companies (
    run_id TEXT NOT NULL
        REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
    company_id TEXT NOT NULL
        REFERENCES companies(company_id) ON DELETE CASCADE,
    discovery_status TEXT NOT NULL CHECK (discovery_status IN (
        'NEW', 'KNOWN', 'UPDATED', 'LEGACY_UNKNOWN'
    )),
    observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    PRIMARY KEY (run_id, company_id)
);

CREATE INDEX idx_branches_company_id ON branches(company_id);
CREATE INDEX idx_company_identities_company_id ON company_identities(company_id);
CREATE INDEX idx_company_identities_lookup
    ON company_identities(identity_type, normalized_value);
CREATE INDEX idx_run_companies_company_id ON run_companies(company_id);
CREATE INDEX idx_run_companies_status ON run_companies(run_id, discovery_status);

CREATE TRIGGER companies_first_seen_immutable
BEFORE UPDATE OF first_seen_at ON companies
WHEN NEW.first_seen_at IS NOT OLD.first_seen_at
BEGIN
    SELECT RAISE(ABORT, 'companies.first_seen_at is immutable');
END;

CREATE TRIGGER companies_discovery_status_immutable
BEFORE UPDATE OF discovery_status ON companies
WHEN NEW.discovery_status IS NOT OLD.discovery_status
BEGIN
    SELECT RAISE(ABORT, 'companies.discovery_status is immutable');
END;

CREATE TRIGGER branches_first_seen_immutable
BEFORE UPDATE OF first_seen_at ON branches
WHEN NEW.first_seen_at IS NOT OLD.first_seen_at
BEGIN
    SELECT RAISE(ABORT, 'branches.first_seen_at is immutable');
END;

CREATE TRIGGER company_identities_first_seen_immutable
BEFORE UPDATE OF first_seen_at ON company_identities
WHEN NEW.first_seen_at IS NOT OLD.first_seen_at
BEGIN
    SELECT RAISE(ABORT, 'company_identities.first_seen_at is immutable');
END;

CREATE TRIGGER legacy_company_cannot_be_new
BEFORE INSERT ON run_companies
WHEN NEW.discovery_status = 'NEW'
 AND (SELECT discovery_status FROM companies WHERE company_id = NEW.company_id)
     = 'LEGACY_UNKNOWN'
BEGIN
    SELECT RAISE(ABORT, 'LEGACY_UNKNOWN company cannot be classified as NEW');
END;

CREATE TRIGGER legacy_company_cannot_become_new
BEFORE UPDATE OF discovery_status ON run_companies
WHEN NEW.discovery_status = 'NEW'
 AND (SELECT discovery_status FROM companies WHERE company_id = NEW.company_id)
     = 'LEGACY_UNKNOWN'
BEGIN
    SELECT RAISE(ABORT, 'LEGACY_UNKNOWN company cannot be classified as NEW');
END;
"""

MIGRATION_2 = """
CREATE TABLE historical_source_records (
    source_record_id TEXT PRIMARY KEY
        CHECK (length(trim(source_record_id)) > 0),
    run_id TEXT NOT NULL
        REFERENCES discovery_runs(run_id) ON DELETE RESTRICT,
    source_path TEXT NOT NULL CHECK (length(trim(source_path)) > 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'GOOGLE_MAPS', 'GOOGLE_SEARCH', 'LEAD_EXPORT', 'DAILY_EXPORT',
        'ENRICHMENT', 'OUTREACH', 'OTHER'
    )),
    source_row_number INTEGER NOT NULL CHECK (source_row_number > 1),
    source_content_hash TEXT NOT NULL
        CHECK (length(source_content_hash) = 64),
    resolution_status TEXT NOT NULL CHECK (resolution_status IN (
        'IMPORTED', 'AMBIGUOUS', 'QUARANTINED'
    )),
    company_id TEXT REFERENCES companies(company_id) ON DELETE RESTRICT,
    branch_id TEXT REFERENCES branches(branch_id) ON DELETE RESTRICT,
    observed_at TEXT,
    reason TEXT,
    raw_record_json TEXT NOT NULL CHECK (length(trim(raw_record_json)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    CHECK (observed_at IS NULL OR length(trim(observed_at)) > 0),
    CHECK (
        (resolution_status = 'IMPORTED'
         AND company_id IS NOT NULL AND branch_id IS NOT NULL)
        OR
        (resolution_status != 'IMPORTED'
         AND company_id IS NULL AND branch_id IS NULL)
    ),
    UNIQUE (source_path, source_row_number, source_content_hash)
);

CREATE INDEX idx_historical_source_records_run
    ON historical_source_records(run_id);
CREATE INDEX idx_historical_source_records_resolution
    ON historical_source_records(resolution_status);
CREATE INDEX idx_historical_source_records_company
    ON historical_source_records(company_id);
CREATE INDEX idx_historical_source_records_branch
    ON historical_source_records(branch_id);
"""

MIGRATION_3 = """
CREATE TABLE branch_identities (
    branch_identity_id TEXT PRIMARY KEY
        CHECK (length(trim(branch_identity_id)) > 0),
    branch_id TEXT NOT NULL
        REFERENCES branches(branch_id) ON DELETE CASCADE,
    identity_type TEXT NOT NULL CHECK (identity_type IN (
        'GOOGLE_MAPS_PLACE_ID', 'NAME', 'PHONE', 'ADDRESS'
    )),
    identity_value TEXT NOT NULL CHECK (length(trim(identity_value)) > 0),
    normalized_value TEXT NOT NULL CHECK (length(trim(normalized_value)) > 0),
    first_seen_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    CHECK (first_seen_at IS NULL OR length(trim(first_seen_at)) > 0),
    CHECK (last_seen_at IS NULL OR length(trim(last_seen_at)) > 0),
    UNIQUE (branch_id, identity_type, normalized_value)
);

CREATE INDEX idx_branch_identities_branch_id
    ON branch_identities(branch_id);
CREATE INDEX idx_branch_identities_lookup
    ON branch_identities(identity_type, normalized_value);
CREATE UNIQUE INDEX idx_branch_place_identity_unique
    ON branch_identities(normalized_value)
    WHERE identity_type = 'GOOGLE_MAPS_PLACE_ID';

CREATE TABLE discovery_observations (
    observation_id TEXT PRIMARY KEY
        CHECK (length(trim(observation_id)) > 0),
    run_id TEXT NOT NULL
        REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
    source_system TEXT NOT NULL CHECK (length(trim(source_system)) > 0),
    source_record_key TEXT NOT NULL CHECK (length(trim(source_record_key)) > 0),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    raw_payload_json TEXT NOT NULL CHECK (length(trim(raw_payload_json)) > 0),
    normalized_evidence_json TEXT NOT NULL
        CHECK (length(trim(normalized_evidence_json)) > 0),
    classification TEXT NOT NULL CHECK (classification IN (
        'NEW', 'KNOWN', 'UPDATED', 'LEGACY_UNKNOWN',
        'AMBIGUOUS', 'QUARANTINED'
    )),
    resolution_action TEXT NOT NULL CHECK (resolution_action IN (
        'CREATE_COMPANY', 'CREATE_BRANCH', 'UPDATE_ENTITY',
        'ADD_PLACE_ALIAS', 'MATCH_ONLY', 'NONE'
    )),
    company_id TEXT REFERENCES companies(company_id) ON DELETE RESTRICT,
    branch_id TEXT REFERENCES branches(branch_id) ON DELETE RESTRICT,
    candidate_company_ids_json TEXT NOT NULL,
    candidate_branch_ids_json TEXT NOT NULL,
    matched_evidence_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    requires_review INTEGER NOT NULL CHECK (requires_review IN (0, 1)),
    resolution_reason TEXT NOT NULL CHECK (length(trim(resolution_reason)) > 0),
    resolver_version TEXT NOT NULL CHECK (length(trim(resolver_version)) > 0),
    observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    CHECK (
        (classification IN ('AMBIGUOUS', 'QUARANTINED')
         AND company_id IS NULL AND branch_id IS NULL
         AND resolution_action = 'NONE' AND requires_review = 1)
        OR
        (classification NOT IN ('AMBIGUOUS', 'QUARANTINED')
         AND company_id IS NOT NULL)
    ),
    CHECK (classification != 'NEW' OR resolution_action = 'CREATE_COMPANY'),
    CHECK (branch_id IS NULL OR company_id IS NOT NULL),
    UNIQUE (source_system, source_record_key, payload_hash)
);

CREATE INDEX idx_discovery_observations_run_classification
    ON discovery_observations(run_id, classification);
CREATE INDEX idx_discovery_observations_company
    ON discovery_observations(company_id);
CREATE INDEX idx_discovery_observations_branch
    ON discovery_observations(branch_id);

CREATE TABLE resolution_reviews (
    review_id TEXT PRIMARY KEY CHECK (length(trim(review_id)) > 0),
    observation_id TEXT NOT NULL
        REFERENCES discovery_observations(observation_id) ON DELETE RESTRICT,
    previous_classification TEXT NOT NULL CHECK (previous_classification IN (
        'NEW', 'KNOWN', 'UPDATED', 'LEGACY_UNKNOWN',
        'AMBIGUOUS', 'QUARANTINED'
    )),
    decided_classification TEXT NOT NULL CHECK (decided_classification IN (
        'NEW', 'KNOWN', 'UPDATED', 'LEGACY_UNKNOWN',
        'AMBIGUOUS', 'QUARANTINED'
    )),
    company_id TEXT REFERENCES companies(company_id) ON DELETE RESTRICT,
    branch_id TEXT REFERENCES branches(branch_id) ON DELETE RESTRICT,
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    decision_reason TEXT NOT NULL CHECK (length(trim(decision_reason)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    CHECK (branch_id IS NULL OR company_id IS NOT NULL)
);

CREATE INDEX idx_resolution_reviews_observation
    ON resolution_reviews(observation_id);

CREATE TRIGGER branches_discovery_status_immutable
BEFORE UPDATE OF discovery_status ON branches
WHEN NEW.discovery_status IS NOT OLD.discovery_status
BEGIN
    SELECT RAISE(ABORT, 'branches.discovery_status is immutable');
END;

CREATE TRIGGER branch_identities_first_seen_immutable
BEFORE UPDATE OF first_seen_at ON branch_identities
WHEN NEW.first_seen_at IS NOT OLD.first_seen_at
BEGIN
    SELECT RAISE(ABORT, 'branch_identities.first_seen_at is immutable');
END;
"""

MIGRATION_4 = """
CREATE TABLE discovery_run_decisions (
    decision_id TEXT PRIMARY KEY
        CHECK (length(trim(decision_id)) > 0),
    run_id TEXT NOT NULL
        REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL
        REFERENCES discovery_observations(observation_id) ON DELETE RESTRICT,
    classification TEXT NOT NULL CHECK (classification IN (
        'NEW', 'KNOWN', 'UPDATED', 'LEGACY_UNKNOWN',
        'AMBIGUOUS', 'QUARANTINED'
    )),
    resolution_action TEXT NOT NULL CHECK (resolution_action IN (
        'CREATE_COMPANY', 'CREATE_BRANCH', 'UPDATE_ENTITY',
        'ADD_PLACE_ALIAS', 'MATCH_ONLY', 'NONE'
    )),
    company_id TEXT REFERENCES companies(company_id) ON DELETE RESTRICT,
    branch_id TEXT REFERENCES branches(branch_id) ON DELETE RESTRICT,
    candidate_company_ids_json TEXT NOT NULL,
    candidate_branch_ids_json TEXT NOT NULL,
    matched_evidence_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    requires_review INTEGER NOT NULL CHECK (requires_review IN (0, 1)),
    resolution_reason TEXT NOT NULL CHECK (length(trim(resolution_reason)) > 0),
    resolver_version TEXT NOT NULL CHECK (length(trim(resolver_version)) > 0),
    observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    CHECK (
        (classification IN ('AMBIGUOUS', 'QUARANTINED')
         AND company_id IS NULL AND branch_id IS NULL
         AND resolution_action = 'NONE' AND requires_review = 1)
        OR
        (classification NOT IN ('AMBIGUOUS', 'QUARANTINED')
         AND company_id IS NOT NULL)
    ),
    CHECK (classification != 'NEW' OR resolution_action = 'CREATE_COMPANY'),
    CHECK (branch_id IS NULL OR company_id IS NOT NULL),
    UNIQUE (run_id, observation_id)
);

CREATE INDEX idx_discovery_run_decisions_run_classification
    ON discovery_run_decisions(run_id, classification);
CREATE INDEX idx_discovery_run_decisions_company
    ON discovery_run_decisions(company_id);
CREATE INDEX idx_discovery_run_decisions_branch
    ON discovery_run_decisions(branch_id);
"""

MIGRATION_5 = """
CREATE TABLE qualification_assessments (
    assessment_id TEXT PRIMARY KEY
        CHECK (length(trim(assessment_id)) > 0),
    decision_id TEXT NOT NULL
        REFERENCES discovery_run_decisions(decision_id) ON DELETE CASCADE,
    company_id TEXT NOT NULL
        REFERENCES companies(company_id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL
        CHECK (length(trim(policy_version)) > 0),
    evidence_hash TEXT NOT NULL CHECK (length(evidence_hash) = 64),
    evidence_json TEXT NOT NULL CHECK (length(trim(evidence_json)) > 0),
    employment_relevance TEXT NOT NULL CHECK (employment_relevance IN (
        'TARGET', 'POSSIBLE', 'NOISE', 'UNKNOWN'
    )),
    mission_relevance TEXT NOT NULL CHECK (mission_relevance IN (
        'TARGET', 'POSSIBLE', 'NOISE', 'UNKNOWN'
    )),
    employment_eligibility TEXT NOT NULL CHECK (employment_eligibility IN (
        'ELIGIBLE', 'REVIEW', 'EXCLUDED'
    )),
    mission_eligibility TEXT NOT NULL CHECK (mission_eligibility IN (
        'ELIGIBLE', 'REVIEW', 'EXCLUDED'
    )),
    employment_reasons_json TEXT NOT NULL
        CHECK (length(trim(employment_reasons_json)) > 0),
    mission_reasons_json TEXT NOT NULL
        CHECK (length(trim(mission_reasons_json)) > 0),
    assessed_at TEXT NOT NULL CHECK (length(trim(assessed_at)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    UNIQUE (decision_id, policy_version)
);

CREATE INDEX idx_qualification_assessments_company
    ON qualification_assessments(company_id);
CREATE INDEX idx_qualification_assessments_employment
    ON qualification_assessments(employment_eligibility, company_id);
CREATE INDEX idx_qualification_assessments_mission
    ON qualification_assessments(mission_eligibility, company_id);
"""

MIGRATION_6 = """
CREATE TABLE discovery_runs_v6 (
    run_id TEXT PRIMARY KEY
        CHECK (length(trim(run_id)) > 0),
    run_type TEXT NOT NULL
        CHECK (run_type IN ('SCRAPE', 'LEGACY_IMPORT', 'MANUAL')),
    status TEXT NOT NULL CHECK (status IN (
        'RUNNING', 'FINALIZING', 'SUCCESS', 'PARTIAL', 'FAILED', 'INTERRUPTED'
    )),
    started_at TEXT NOT NULL CHECK (length(trim(started_at)) > 0),
    finished_at TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    new_company_limit INTEGER CHECK (
        new_company_limit IS NULL OR new_company_limit >= 1
    ),
    run_config_hash TEXT CHECK (
        run_config_hash IS NULL OR length(run_config_hash) = 64
    ),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR length(trim(lease_owner)) > 0
    ),
    lease_expires_at TEXT CHECK (
        lease_expires_at IS NULL OR length(trim(lease_expires_at)) > 0
    ),
    heartbeat_at TEXT CHECK (
        heartbeat_at IS NULL OR length(trim(heartbeat_at)) > 0
    ),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        CHECK (length(trim(updated_at)) > 0),
    CHECK (finished_at IS NULL OR length(trim(finished_at)) > 0),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

INSERT INTO discovery_runs_v6
    (run_id, run_type, status, started_at, finished_at, created_at, updated_at)
SELECT run_id, run_type, status, started_at, finished_at, created_at,
       COALESCE(finished_at, created_at)
  FROM discovery_runs;

DROP TABLE discovery_runs;
ALTER TABLE discovery_runs_v6 RENAME TO discovery_runs;
"""

MIGRATIONS = (
    (1, MIGRATION_1),
    (2, MIGRATION_2),
    (3, MIGRATION_3),
    (4, MIGRATION_4),
    (5, MIGRATION_5),
    (6, MIGRATION_6),
)
