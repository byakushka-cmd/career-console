-- Additive: which import run produced the data, and the provisional (derived, non-authoritative)
-- state assigned to each legacy row. Original source text stays in legacy_rows.values_json.
CREATE TABLE import_batches (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES source_snapshots(id),
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT NOT NULL,
    tool TEXT NOT NULL,
    legacy_rows INTEGER NOT NULL,
    notes TEXT NOT NULL DEFAULT '');
CREATE TABLE legacy_row_states (
    legacy_row_id INTEGER PRIMARY KEY REFERENCES legacy_rows(id),
    provisional_state TEXT NOT NULL,
    classifier TEXT NOT NULL);
