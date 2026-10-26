-- Potential Leads: hiring posts, recruiters, referrals captured by hand (no scraping). One row per post
-- identity; every capture of it is kept as an observation. Decisions go to decision_events ('lead').
CREATE TABLE leads (
    id INTEGER PRIMARY KEY,
    identity TEXT NOT NULL UNIQUE,     -- e.g. linkedin-activity:7251…, else the normalised URL
    post_url TEXT NOT NULL,
    kind TEXT NOT NULL,                -- POST_LEAD / RECRUITER_LEAD / REFERRAL / OTHER
    author TEXT NOT NULL DEFAULT '',
    author_role TEXT NOT NULL DEFAULT '',
    author_url TEXT,
    company TEXT NOT NULL DEFAULT '',
    role_signal TEXT NOT NULL DEFAULT '',
    job_location TEXT NOT NULL DEFAULT '',  -- where the JOB is, as the post says; the author's location never counts
    posted_on TEXT,
    excerpt TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL);

CREATE TABLE lead_observations (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    captured_at_utc TEXT NOT NULL,
    post_url TEXT NOT NULL,
    job_location TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '');
CREATE INDEX idx_lead_observations_lead ON lead_observations(lead_id, id);
