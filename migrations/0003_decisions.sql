-- Append-only decision log. Every review resolution (and, later, every personal decision or
-- status change) is a new row; rows are never edited or deleted, so history cannot be lost.
CREATE TABLE decision_events (
    id INTEGER PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'web');
CREATE INDEX idx_decision_subject ON decision_events(subject_type, subject_id, id);
CREATE TRIGGER decision_events_no_update BEFORE UPDATE ON decision_events
BEGIN SELECT RAISE(ABORT, 'decision_events is append-only'); END;
CREATE TRIGGER decision_events_no_delete BEFORE DELETE ON decision_events
BEGIN SELECT RAISE(ABORT, 'decision_events is append-only'); END;

-- Imported evidence is immutable from now on.
CREATE TRIGGER legacy_rows_no_update BEFORE UPDATE ON legacy_rows
BEGIN SELECT RAISE(ABORT, 'legacy_rows is immutable source evidence'); END;
CREATE TRIGGER legacy_rows_no_delete BEFORE DELETE ON legacy_rows
BEGIN SELECT RAISE(ABORT, 'legacy_rows is immutable source evidence'); END;
CREATE TRIGGER source_snapshots_no_update BEFORE UPDATE ON source_snapshots
BEGIN SELECT RAISE(ABORT, 'source_snapshots is immutable source evidence'); END;
CREATE TRIGGER source_snapshots_no_delete BEFORE DELETE ON source_snapshots
BEGIN SELECT RAISE(ABORT, 'source_snapshots is immutable source evidence'); END;
