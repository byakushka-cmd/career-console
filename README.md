# Career Console

A job-search console that runs on your own computer. It finds new jobs on employers' official career sites and
in your job-alert e-mails, keeps every application in one list with its history, and reads the employers'
replies for you: a confirmation marks the application as sent, a rejection closes it.

Everything stays on your computer: one small database, no account, no cloud, no AI, no paid services. It is
built for a job search in Israel (Hebrew and English job titles, Israeli job boards and regions) and works in
English, with Hebrew and Russian in titles, notes and dictation.

## What it does

- **New Opportunities.** Checks about a hundred official career sites (Greenhouse, Lever, Ashby, Comeet,
  Workday) every few hours and shows the jobs that match your search terms, are in Israel (in the regions you
  choose) and are not in your list yet. Lead roles first. Each job has its link to the employer's own posting,
  a search for it on the employer's site and contacts to look for on LinkedIn.
- **Job-alert e-mails.** Reads the alerts LinkedIn, AllJobs and Drushim send you, from your own mailbox
  (read-only), and puts their jobs through the same rules.
- **My Pipeline.** All your applications: status, history, notes, the same job seen through different sources.
  Lead roles and engineer roles apart, status groups, search. Add an application in seconds: paste a link and a
  few words, or dictate them.
- **Replies.** Employers' "we got your application" and "we decided not to move forward" e-mails update the
  status by themselves; a reply never overwrites your own later update.
- **Filters you can see.** Every rule that hides a job (excluded title words, regions, junior positions,
  already known…) is listed with how many jobs it holds back, and can be turned off.

## Install (macOS)

Open **Terminal** and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/byakushka-cmd/career-console/main/install.sh | bash
```

It installs [uv](https://docs.astral.sh/uv/) (which brings Python), puts the console in `~/CareerConsole`,
creates an empty database, adds the official career sites of Israeli tech companies, puts three icons on your
desktop and opens the console in your browser. It takes a few minutes the first time.

From then on, use the icons on your desktop:

| Icon | What it does |
|---|---|
| **Career Console** | opens the console (starts it if needed) |
| **Search Jobs Now** | checks the career sites now and shows a notification |
| **Stop Career Console** | stops it |

To update to a new version, run the same command again. Your data stays as it is.

### Manual install (Linux, Windows, or without the script)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and git, then:

```bash
git clone https://github.com/byakushka-cmd/career-console.git
cd career-console
uv sync
uv run career-console init
uv run career-console boards discover
uv run career-console start
```

The console opens at http://127.0.0.1:45627. On Linux and Windows, `uv run career-console serve` runs it in the
terminal (Ctrl+C stops it); desktop icons and the automatic schedule are macOS only.

## First steps

1. **Search settings** (the link at the top of New Opportunities): your search terms, excluded words, your
   level, the regions of Israel that are too far for you. The defaults are for QA and test automation, lead and
   senior roles: change them to your field.
2. **Job-alert e-mails**, in Search settings. Connect the Gmail where your LinkedIn, AllJobs or Drushim alerts
   arrive. Gmail needs an *app password*, not your normal password: turn on 2-Step Verification at
   [myaccount.google.com/security](https://myaccount.google.com/security), then create an app password named
   "Career Console" at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and paste it
   into the form. The mailbox is opened read-only: nothing is marked, moved or deleted. The password is kept only
   on your computer, in `data/mail.env`.
3. **Your applications so far:** "Add an application" at the top of My Pipeline. The search then never shows
   them to you as new.
4. **Automatic search** (macOS): every two hours from 8:00 to 22:00, with

   ```bash
   cd ~/CareerConsole && uv run career-console schedule --install
   ```

   Optionally, new jobs can also come to you by e-mail or Telegram: copy `examples/notify.env.example` to
   `data/notify.env`, fill it in, and check it with `uv run career-console notify --test`.

More about how the search decides what to show: [docs/SEARCH.md](docs/SEARCH.md).

## Your data

Everything the console knows is in the `data/` folder inside its folder (`~/CareerConsole/data`): the database,
your mailbox's app password, your settings, backups and logs. It is never uploaded anywhere and never part of
the code you download. The console listens only on your computer (127.0.0.1) and contacts only the career sites
it checks and your own mailbox.

```bash
uv run career-console backup
```

```bash
uv run career-console restore data/backups/career_console-YYYYMMDD-HHMMSS.sqlite
```

A backup is one self-contained file in `data/backups/`; `restore` first saves the current database, so nothing
is lost. To move to another computer, copy the whole folder including `data/`, privately (never to a public
place).

## Uninstall

```bash
cd ~/CareerConsole && uv run career-console stop && uv run career-console desktop --uninstall && uv run career-console schedule --uninstall
```

Then delete the `~/CareerConsole` folder (this deletes your data too: back it up first if you want to keep it).

## For developers

```
app/         web app (FastAPI + Jinja), CLI, import, backup, desktop icons, templates and styles
search/      the search: settings and rules, career-site readers, alert e-mails, replies, leads
migrations/  numbered SQL migrations (the console upgrades its database with `career-console migrate`)
tests/       pytest suite on generated data
docs/        how the search works (SEARCH.md), importing an existing tracker (IMPORT.md)
data/        your private data: never in git
```

```bash
uv run pytest
```

An existing tracker kept in Google Sheets can be imported instead of starting empty: see
[docs/IMPORT.md](docs/IMPORT.md).
