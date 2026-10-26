-- Baseline: the Phase 2 dry-run schema, unchanged (DDL copied verbatim from
-- CareerConsole_Migration_DryRun.sqlite so the two databases can be compared table by table).
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at_utc TEXT NOT NULL);

CREATE TABLE source_snapshots (
            id INTEGER PRIMARY KEY, file_name TEXT NOT NULL, sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, source_file_id TEXT NOT NULL, source_url TEXT NOT NULL,
            source_last_modified_utc TEXT NOT NULL, source_timezone TEXT NOT NULL,
            exported_at_utc TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode='READ_ONLY_DRY_RUN'));
CREATE TABLE legacy_rows (
            id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id),
            sheet_name TEXT NOT NULL, source_row INTEGER NOT NULL, values_json TEXT NOT NULL,
            row_sha256 TEXT NOT NULL, UNIQUE(snapshot_id,sheet_name,source_row));
CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL, key TEXT UNIQUE NOT NULL);
CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
            title TEXT NOT NULL, status_hint TEXT, identity_policy TEXT NOT NULL,
            is_manual_review INTEGER NOT NULL DEFAULT 0);
CREATE TABLE job_legacy_links (
            job_id INTEGER NOT NULL REFERENCES jobs(id), legacy_row_id INTEGER NOT NULL UNIQUE REFERENCES legacy_rows(id),
            reason TEXT NOT NULL, PRIMARY KEY(job_id,legacy_row_id));
CREATE TABLE job_sources (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
            legacy_row_id INTEGER NOT NULL REFERENCES legacy_rows(id), url TEXT NOT NULL,
            identity TEXT, UNIQUE(job_id,legacy_row_id,url));
CREATE TABLE applications (
            id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id), company_id INTEGER NOT NULL REFERENCES companies(id),
            state TEXT NOT NULL, applied_on TEXT, current_status_raw TEXT NOT NULL,
            application_type TEXT NOT NULL, provenance TEXT NOT NULL,
            review_required INTEGER NOT NULL DEFAULT 0);
CREATE TABLE application_sources (
            application_id INTEGER NOT NULL REFERENCES applications(id), legacy_row_id INTEGER NOT NULL REFERENCES legacy_rows(id),
            PRIMARY KEY(application_id,legacy_row_id));
CREATE TABLE history_events (
            id INTEGER PRIMARY KEY, application_id INTEGER REFERENCES applications(id),
            legacy_row_id INTEGER NOT NULL REFERENCES legacy_rows(id),
            event_type TEXT NOT NULL, event_text TEXT NOT NULL, event_time TEXT,
            provenance TEXT NOT NULL);
CREATE TABLE contacts (id INTEGER PRIMARY KEY, display_name TEXT NOT NULL, key TEXT UNIQUE NOT NULL,
            requires_review INTEGER NOT NULL DEFAULT 1);
CREATE TABLE contact_mentions (
            id INTEGER PRIMARY KEY, legacy_row_id INTEGER NOT NULL REFERENCES legacy_rows(id),
            company_id INTEGER NOT NULL REFERENCES companies(id), raw_contact TEXT NOT NULL,
            contact_id INTEGER REFERENCES contacts(id));
CREATE TABLE discovery_records (
            id INTEGER PRIMARY KEY, legacy_row_id INTEGER NOT NULL REFERENCES legacy_rows(id),
            job_id INTEGER REFERENCES jobs(id), source_status TEXT, verified_raw TEXT, note_raw TEXT);
CREATE TABLE screening_rules (
            id INTEGER PRIMARY KEY, legacy_row_id INTEGER NOT NULL REFERENCES legacy_rows(id),
            rule_name TEXT NOT NULL, guidance TEXT NOT NULL);
CREATE TABLE migration_issues (
            id INTEGER PRIMARY KEY, legacy_row_id INTEGER REFERENCES legacy_rows(id),
            severity TEXT NOT NULL, code TEXT NOT NULL, details TEXT NOT NULL, suggested_action TEXT NOT NULL);
CREATE TABLE duplicate_reviews (
            id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id),
            status TEXT NOT NULL, confidence TEXT NOT NULL, reason TEXT NOT NULL,
            first_legacy_row INTEGER REFERENCES legacy_rows(id), second_legacy_row INTEGER REFERENCES legacy_rows(id));
CREATE INDEX idx_legacy_sheet ON legacy_rows(sheet_name,source_row);
CREATE INDEX idx_appl_state ON applications(state);
CREATE INDEX idx_jobs_company ON jobs(company_id);
CREATE INDEX idx_issues_code ON migration_issues(code);
