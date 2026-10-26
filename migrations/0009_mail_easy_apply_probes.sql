-- LinkedIn marks a job "Apply with resume & profile" (Easy Apply) in its alerts: the weakest way to apply, so the
-- console says so and points to the employer's own page first.
ALTER TABLE mail_jobs ADD COLUMN easy_apply INTEGER NOT NULL DEFAULT 0;

-- Each company named in your alerts is checked once for a public Greenhouse / Lever / Ashby board; a board with
-- jobs in Israel joins the boards the search reads, so the employer's own posting is found from then on.
CREATE TABLE mail_company_probes (
    company_key TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    probed_at_utc TEXT NOT NULL,
    provider TEXT,
    board TEXT,
    israel_jobs INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '');
