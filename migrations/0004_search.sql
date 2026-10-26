-- Job search ("sniffer"): a configurable profile, official ATS boards, runs, postings, and each
-- candidate's path Discovery -> Deduplication -> Verification -> Relevance -> Presentation.
-- Kept apart from the imported tracker tables, which stay exactly as imported.

CREATE TABLE search_profile_versions (
    id INTEGER PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    origin TEXT NOT NULL);
CREATE TRIGGER search_profile_versions_no_update BEFORE UPDATE ON search_profile_versions
BEGIN SELECT RAISE(ABORT, 'search_profile_versions is append-only'); END;
CREATE TRIGGER search_profile_versions_no_delete BEFORE DELETE ON search_profile_versions
BEGIN SELECT RAISE(ABORT, 'search_profile_versions is append-only'); END;

CREATE TABLE search_sources (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    board TEXT NOT NULL,
    company TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL,
    added_at_utc TEXT NOT NULL,
    UNIQUE(provider, board));

CREATE TABLE search_runs (
    id INTEGER PRIMARY KEY,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    status TEXT NOT NULL,              -- running / finished / failed / abandoned
    mode TEXT NOT NULL,                -- live / dry-run
    trigger TEXT NOT NULL,             -- cli / web / schedule
    profile_version_id INTEGER REFERENCES search_profile_versions(id),
    sources_total INTEGER NOT NULL DEFAULT 0,
    sources_ok INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    postings_seen INTEGER NOT NULL DEFAULT 0,
    postings_new INTEGER NOT NULL DEFAULT 0,
    presented INTEGER NOT NULL DEFAULT 0,
    paid_calls INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    stopped_by_budget INTEGER NOT NULL DEFAULT 0,
    error TEXT);

CREATE TABLE source_checks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES search_runs(id),
    source_id INTEGER NOT NULL REFERENCES search_sources(id),
    checked_at_utc TEXT NOT NULL,
    ok INTEGER NOT NULL,
    http_status INTEGER,
    postings INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    duration_ms INTEGER);
CREATE INDEX idx_source_checks_source ON source_checks(source_id, id);

CREATE TABLE postings (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES search_sources(id),
    identity TEXT NOT NULL UNIQUE,     -- provider-level job ID, e.g. greenhouse:5004111101
    provider TEXT NOT NULL,
    provider_job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    countries TEXT NOT NULL DEFAULT '',
    remote INTEGER,
    workplace TEXT,
    url TEXT NOT NULL,
    apply_url TEXT,
    requisition_id TEXT,
    posted_at TEXT,                    -- as published by the source; NULL when it does not say
    source_updated_at TEXT,
    description TEXT NOT NULL DEFAULT '',
    description_sha TEXT NOT NULL,
    first_seen_at_utc TEXT NOT NULL,
    last_seen_at_utc TEXT NOT NULL,
    first_run_id INTEGER REFERENCES search_runs(id),
    last_run_id INTEGER REFERENCES search_runs(id));

CREATE TABLE run_candidates (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES search_runs(id),
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    is_new INTEGER NOT NULL,
    dedup TEXT NOT NULL,               -- NEW / KNOWN_NOT_APPLIED / HISTORY / POSSIBLE_HISTORY
    verification TEXT NOT NULL,        -- VERIFIED_ACTIONABLE (listed on the official board in this run)
    relevance TEXT NOT NULL,           -- MATCH / EXCLUDED / NO_MATCH
    geo TEXT NOT NULL,                 -- ISRAEL / WORLDWIDE_REMOTE / OUTSIDE / UNKNOWN
    presented INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    UNIQUE(run_id, posting_id));
CREATE INDEX idx_run_candidates_posting ON run_candidates(posting_id, run_id);

CREATE TABLE cost_ledger (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES search_runs(id),
    created_at_utc TEXT NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    reserved_usd REAL NOT NULL DEFAULT 0,
    actual_usd REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '');
