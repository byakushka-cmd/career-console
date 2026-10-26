-- Jobs from your job-alert e-mails (LinkedIn, AllJobs): they send the alerts to you, and the console reads them
-- from your mailbox instead of visiting those sites. The mailbox is opened read-only: nothing in it is marked,
-- moved or deleted. One row per job, however many e-mails named it; decisions go to decision_events
-- ('mail_job'), like every other decision.
CREATE TABLE mail_jobs (
    id INTEGER PRIMARY KEY,
    identity TEXT NOT NULL UNIQUE,      -- linkedin:4478187296, alljobs:8858223
    source TEXT NOT NULL,               -- LinkedIn / AllJobs
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    first_seen_at_utc TEXT NOT NULL,    -- when the first e-mail naming it arrived
    last_seen_at_utc TEXT NOT NULL,
    mails INTEGER NOT NULL DEFAULT 1);

-- Each e-mail is read once (by its Message-ID): reading the last two days every two hours never counts twice.
CREATE TABLE mail_messages (
    message_id TEXT PRIMARY KEY,
    mailbox TEXT NOT NULL,
    sender TEXT NOT NULL,
    received_at_utc TEXT,
    jobs INTEGER NOT NULL DEFAULT 0,
    read_at_utc TEXT NOT NULL);

-- Every check of a mailbox, for the status line and for errors (a wrong app password, no connection).
CREATE TABLE mail_reads (
    id INTEGER PRIMARY KEY,
    read_at_utc TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    ok INTEGER NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    jobs INTEGER NOT NULL DEFAULT 0,
    new_jobs INTEGER NOT NULL DEFAULT 0,
    error TEXT);
