# Career Console

A small, local-first job-search workspace for one person: one app, one SQLite database,
three views (**New Opportunities** with *Verified Jobs* / *Potential Leads* tabs, **My Pipeline**,
**Diagnostics**). Runs on your own computer at `http://127.0.0.1:45627`. No cloud, no accounts, no paid APIs.

**Status: Milestone 2 + job search + leads** — validated import of the 14 Oct 2026 Google Sheets snapshot, a local
console, a review queue (every decision appended to a log; nothing overwritten), and a job search that checks
employers' official boards and fills **Verified Jobs**. Editing records and Potential Leads capture come next.

The original Google Sheet is never written to. The app has no code that talks to Google, or to any network service.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) (`uv` installs Python and the locked dependencies)
- macOS, Linux or Windows

## First-time setup

```bash
uv sync
```

### Start from scratch

No tracker to bring along: create an empty database, add official job boards, open the console.

```bash
uv run career-console init
uv run career-console boards
uv run career-console start
```

Then, in the console: Search settings (what to look for, your regions, your job-alert mailbox), and
"Add an application" on My Pipeline for applications you already sent.

### Or import an existing tracker

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

## Updating to a new version

After pulling a new version of the code, upgrade the database before starting the server:

```bash
uv run career-console migrate
```

It makes a verified backup first, applies the new schema migrations, queues any new possible
duplicates for review (never merging them), and checks that no existing row changed. The server
refuses to start on an outdated schema and tells you to run this.

## Daily use

On macOS the simplest way is the desktop shortcuts (create them once with `uv run career-console desktop`):
**Career Console** opens the console (starting it in the background if needed), **Search Jobs Now** checks the
boards and shows a notification, **Stop Career Console** stops it. The same from a terminal:

```bash
uv run career-console start
```

```bash
uv run career-console stop
```

Or run it in the foreground (Ctrl+C to stop):

```bash
uv run career-console serve
```

Open http://127.0.0.1:45627 (a fixed port, "4" + JOBS on a phone keypad). The server listens on this computer only and refuses LAN addresses.
Access logs are off because search terms can contain contact names. The theme button in the header
switches dark/light (remembered in this browser).

**Add an application** (top of My Pipeline): for a job the console did not bring you — applied on LinkedIn, through
a recruiter, from a friend. Give a link and a few words: paste the job card, or press **Dictate** and speak
(Russian, Hebrew or English; your browser recognises the speech — Safari through Apple, Chrome through Google —
and macOS dictation, Fn twice, works in the field too). The console reads the company, the role, the job ID and
where the link points, with fixed rules (no AI); press **Applied** or **Interested**. The record joins My Pipeline
(code *AD*), gets statuses like any tracker record, and the search treats it as known: the same job never comes
back as new. Whatever was read wrongly is corrected on its page; the old value is kept.

**Review queue** (Diagnostics): items that need your decision — malformed rows, stale index flags,
possible duplicates. Each has a short note field and buttons such as *Keep as is*, *Same requisition*
or *Different requisitions*. Decisions are appended to `decision_events`, which cannot be edited or
deleted (enforced by database triggers); *Reopen* adds a new event rather than undoing the old one.
Forms only accept posts from the console's own pages (cross-site requests are refused).

## Job search

```bash
uv run career-console search
```

Or press **Run search now** in New Opportunities. Automatic runs, notifications (e-mail or Telegram) and finding
more boards: see [docs/SEARCH.md](docs/SEARCH.md). In short:

```bash
uv run career-console schedule --install --every 2 --from 8 --to 22
```

```bash
uv run career-console notify --test
```
 It checks the official Greenhouse / Lever / Ashby / Comeet
boards in **Search settings** (seeded from the links in your tracker), and shows jobs that are live, match your
terms, are in Israel or worldwide-remote, and that you have not already applied to. Free, no AI, no API keys.
Rules, limits and an optional schedule: [docs/SEARCH.md](docs/SEARCH.md).

## Backup and restore

```bash
uv run career-console backup
```

```bash
uv run career-console restore data/backups/career_console-YYYYMMDD-HHMMSS.sqlite
```

Backups use SQLite's backup API, are integrity-checked, are single self-contained files, and are saved with owner-only permissions.
`restore` first saves the current database as a `-pre-restore` backup, so nothing is lost. Stop the server before restoring.

## Moving to another computer

Copy the whole `career-console/` folder, **including `data/`**, privately (encrypted USB drive or a
private cloud folder; never a public repository). Then on the new machine: `uv sync` and `serve`.
`data/` is self-contained: snapshot, metadata, database, backups and reports.

## Layout

```
app/            FastAPI app, importer, validation, review queue, backup, CLI, templates, static CSS
search/         job search: profile and gates, official board adapters, run pipeline
migrations/     numbered SQL migrations; each runs in one transaction with its schema_version row
tests/          pytest suite; synthetic data generated at test time (no real data in git)
examples/       example metadata file
docs/           audit notes
data/           PRIVATE, gitignored: source/, reference/, career_console.sqlite, backups/, reports/
```

## Tests

```bash
uv run pytest
```

Tests marked `real_data` run only when the private snapshot is present in `data/source/` and are
skipped elsewhere. Everything else uses synthetic, generated data.

## Data and privacy rules

- `data/`, `*.sqlite`, `*.xlsx`, `*.csv`, `*.meta.json` and `.env` are gitignored; a test fails if any becomes committable, or if your sheet ID or name appears in a committable file.
- Database, backups and reports are created with owner-only permissions; the snapshot copy is read-only.
- Pages are sent with a strict Content-Security-Policy, `no-referrer` and `no-store`.
- Nothing is published anywhere. A redacted read-only mobile summary is a later, opt-in step.

## What is implemented vs not yet

| Area | State |
|---|---|
| Snapshot import (portable, stdlib XLSX reader, non-destructive) | implemented, validated against Phase 2 |
| Schema migrations with version table | implemented (v1 Phase 2 baseline, v2 import provenance, v3 decision log + immutable evidence) |
| Backup / restore; `migrate` with automatic backup and row-count check | implemented |
| Review queue: issues and duplicates resolved by append-only decisions | implemented |
| Possible duplicates (same company and title, no shared ID) queued, never merged | implemented |
| Baseline status mapping (original text → provisional state) | shown in Diagnostics, read-only |
| My Pipeline: five tiles (New discoveries · Not applied yet · On hold · In progress · Closed), search, status groups, record detail with original text and provenance; not applied within two weeks → Expired | implemented |
| Diagnostics: health, review queue, status mapping, screening rules | implemented |
| New jobs: official-board search, history dedup (IDs, core titles, same-text copies), Israel gate, freshness, your level first, possible contacts; Good find — applied / Save / Maybe / Duplicate / Wrong match / Not for me / Hide | implemented (Greenhouse, Lever, Ashby, Comeet) |
| Search settings: editable, versioned profile; add / turn off boards | implemented |
| Search runs in Diagnostics: per-board checks, every posting's reason, cost ($0) | implemented |
| Potential Leads: paste a post link + what it says; strict Israel gate on the job location; merge repeats; LinkedIn search links you run yourself | implemented (no scraping, no automated messages) |
| Your links (LinkedIn profile, CV): copy button on every page, kept only on this computer | implemented |
| Tracker records: update status (Gone / stale, Not a fit, Applied, Interview, Rejected by them, …) with note and Undo; "Already applied" on a posting carries over to tracker rows with the same official ID | implemented |
| Freshness: New badge (appeared since the board's previous check), Fresh ≤ 7 days; only jobs published within 14 days are shown as new; known jobs published again show as Reposted | implemented |
| Add an application yourself: link + pasted or dictated text → company, role, job ID read by rules; statuses; known to the search | implemented (schema v7) |
| Job-alert e-mails: LinkedIn, AllJobs and Drushim alerts read from your mailbox (read-only IMAP, app password in data/mail.env), same rules as the boards, one click into your tracker | implemented (schema v8) |
| Replies to your applications: confirmations set Applied, rejections set Rejected by them, read from the same mailboxes (rules, no AI; unplaced ones listed) | implemented (schema v10) |
| Editing other tracker fields (next action, notes, labels); converting a lead into a job | not yet |
| Decisions changing job identity (applying "same / different requisition") | recorded only; applied when search needs it (Milestone 4) |
| Workday boards (site search filtered to Israel, newest first, capped) | implemented |
| SmartRecruiters / Workable boards; budget caps for paid sources; scheduling installer | not yet (scheduling: manual recipe in docs/SEARCH.md) |
| Read-only mobile export | not yet (Milestone 5) |
