-- Replies to your applications read from your mailbox (an employer's confirmation or its no): each e-mail once,
-- with what the console did about it (search/replies.py). Nothing here changes your tracker by itself: the
-- changes are ordinary status events.
CREATE TABLE mail_replies (
    message_id TEXT PRIMARY KEY,
    mailbox TEXT NOT NULL,
    received_at_utc TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,              -- applied | rejected
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    record_ref TEXT,                 -- the tracker record it is about (MT 12, AD 3, CC 45), when found
    record_href TEXT,                -- its page in the console
    action TEXT NOT NULL,            -- what the console did, in words
    read_at_utc TEXT NOT NULL);
CREATE INDEX idx_mail_replies_received ON mail_replies(received_at_utc);
