-- Notifications about new verified jobs: every attempt is logged; a posting is sent at most once.
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    channel TEXT,
    status TEXT NOT NULL,          -- sent / failed
    items INTEGER NOT NULL DEFAULT 0,
    error TEXT);
CREATE TABLE notified_postings (
    posting_id INTEGER PRIMARY KEY REFERENCES postings(id),
    notification_id INTEGER NOT NULL REFERENCES notifications(id));
