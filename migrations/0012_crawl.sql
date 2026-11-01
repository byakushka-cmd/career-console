-- New companies found once a day through Common Crawl's public index (search/crawl.py): the lists already read,
-- the boards they named with what reading each one showed, and the days it ran.
CREATE TABLE crawl_reads (
    place TEXT NOT NULL,              -- the address asked about, e.g. comeet.com/jobs/
    crawl TEXT NOT NULL,              -- the archive, e.g. CC-MAIN-2026-39
    read_at_utc TEXT NOT NULL,
    boards INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (place, crawl));
CREATE TABLE crawl_boards (
    provider TEXT NOT NULL,
    key TEXT NOT NULL,                -- the same board under another address has the same key
    board TEXT NOT NULL,
    found_in TEXT NOT NULL,
    found_at_utc TEXT NOT NULL,
    checked_at_utc TEXT,
    jobs INTEGER NOT NULL DEFAULT 0,
    israel_jobs INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider, key));
CREATE TABLE crawl_days (
    day TEXT PRIMARY KEY,             -- one a day, in local time
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    added INTEGER NOT NULL DEFAULT 0,
    problems TEXT NOT NULL DEFAULT '');
