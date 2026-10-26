-- Applications and jobs you add yourself in the console (a link and a few words, pasted or dictated).
-- The imported tracker stays as it was; these records grow your pipeline from now on. Their status is the
-- latest decision event (subject_type 'added'), like every other record; a field you correct is updated here
-- and the old value is kept in an 'edited' event.
CREATE TABLE added_records (
    id INTEGER PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    url TEXT,
    pasted TEXT NOT NULL DEFAULT '',       -- what you pasted or dictated, kept as written
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    job_ids TEXT NOT NULL DEFAULT '',      -- vacancy numbers, comma-separated
    location TEXT);
