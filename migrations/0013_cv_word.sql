-- A CV kept as a Word file: the console changes the title in the .docx and makes the PDF from it with LibreOffice.
ALTER TABLE cv_files ADD COLUMN pdf_path TEXT;      -- the PDF of an uploaded Word file, in data/cv
ALTER TABLE cv_versions ADD COLUMN pdf_path TEXT;   -- the PDF of a copy made from a Word file
-- The text of a job, pasted to see its first requirements: for a job in the console (by its reference) or one
-- pasted on My CV only ("JOB 3").
CREATE TABLE cv_jobs (
    id INTEGER PRIMARY KEY,
    record_ref TEXT UNIQUE,
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    updated_at_utc TEXT NOT NULL);
