# Importing an existing tracker

For a start with the history you already have: a Google Sheets tracker exported as XLSX with the console's sheet
layout (a *Master Tracker* sheet and an optional *Comeet Index* sheet). Without such a file, start from scratch
(`career-console init`) and add your applications on My Pipeline instead.

Put the snapshot and its metadata in the private `data/` folder, then import (the importer copies the
snapshot into `data/source/` read-only if you point it elsewhere):

```bash
uv run career-console import --snapshot data/source/CareerConsole_SourceSnapshot_2026-10-14.xlsx
```

```bash
uv run career-console validate
```

`validate` re-reads the snapshot and checks the database: integrity, foreign keys, every row stored
verbatim (row SHA-256), blank cells, dates, hyperlinks, the Phase 2 baseline counts, and — if
`data/reference/*.sqlite` exists — a table-by-table comparison with the Phase 2 dry-run. The report is
written to `data/reports/`.

The metadata file sits next to the snapshot as `<snapshot name>.meta.json`
(see `examples/snapshot_meta.example.json`). It holds facts that are not inside the XLSX and are
private (sheet ID, your name as used in statuses), so it is never committed.

The import never changes the spreadsheet: it is read once, every row is kept verbatim (with its SHA-256), and
the console's own updates are separate events on top of it.
