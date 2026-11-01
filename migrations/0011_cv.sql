-- Your CV: the PDFs you upload, and the copies made for one job with only the header lines changed (app/cv.py).
-- The files themselves are next to the database, in data/cv.
CREATE TABLE cv_files (
    id INTEGER PRIMARY KEY,
    uploaded_at_utc TEXT NOT NULL,
    name TEXT NOT NULL,               -- the file's name when you uploaded it
    path TEXT NOT NULL,               -- its name in data/cv
    sha256 TEXT NOT NULL UNIQUE,
    top_lines TEXT NOT NULL,          -- the first lines of the page, as read: [[line number, text], ...]
    header_lines TEXT NOT NULL,       -- which of them may change for a job: [line number, ...]
    removed_at_utc TEXT);
CREATE TABLE cv_versions (
    id INTEGER PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    cv_id INTEGER NOT NULL REFERENCES cv_files(id),
    record_ref TEXT NOT NULL,         -- the job it was made for (AD 3, MT 12, CC 45)
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    job_href TEXT NOT NULL DEFAULT '',-- that job's page in the console
    lines TEXT NOT NULL,              -- the header lines of this copy: {line number: text}
    path TEXT NOT NULL,
    origin TEXT NOT NULL);
CREATE INDEX idx_cv_versions_ref ON cv_versions(record_ref, id);
