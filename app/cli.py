"""Command line: import, validate, migrate, search, notify, schedule, backup, restore, serve."""
from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import socket
import sys
from pathlib import Path

from app import backup as backup_mod
from app import legacy_import, upgrade, validation
from app.config import ensure_private_dir, paths

# Fixed and deliberately uncommon, so it never collides with other local projects:
# "4" + JOBS on a phone keypad. Below 49152, so macOS never hands it out as a temporary port.
DEFAULT_PORT = 45627


def _snapshot_in_store(snapshot: Path) -> Path:
    """Keep an immutable copy of the snapshot (and its metadata sidecar) in data/source."""
    store = ensure_private_dir(paths().source)
    target = store / snapshot.name
    if snapshot.resolve() == target.resolve():
        return target
    if target.exists():
        if legacy_import.file_sha256(target) != legacy_import.file_sha256(snapshot):
            sys.exit(f"{target} already exists with different content; refusing to replace a stored snapshot.")
    else:
        shutil.copyfile(snapshot, target)
        target.chmod(0o444)
    meta_src, meta_dst = legacy_import.meta_path_for(snapshot), legacy_import.meta_path_for(target)
    if meta_src.exists() and not meta_dst.exists():
        shutil.copyfile(meta_src, meta_dst)
        meta_dst.chmod(0o600)
    return target


def _default_snapshot() -> Path | None:
    found = sorted(paths().source.glob("*.xlsx"))
    return found[-1] if found else None


def _default_reference() -> Path | None:
    found = sorted(paths().reference.glob("*.sqlite"))
    return found[-1] if found else None


def cmd_import(args) -> None:
    snapshot = _snapshot_in_store(Path(args.snapshot))
    meta_file = Path(args.meta) if args.meta else legacy_import.meta_path_for(snapshot)
    if not meta_file.exists():
        sys.exit(f"Missing snapshot metadata {meta_file} (see examples/snapshot_meta.example.json).")
    db_path = Path(args.db) if args.db else paths().db
    ensure_private_dir(db_path.parent)
    result = legacy_import.import_snapshot(snapshot, legacy_import.SnapshotMeta.load(meta_file), db_path)
    print(f"Imported {snapshot.name} (sha256 {result.snapshot_sha256[:12]}…) into {db_path}")
    for table, n in result.counts.items():
        print(f"  {table:20s} {n}")
    print("Next: career-console validate")


def cmd_validate(args) -> None:
    db_path = Path(args.db) if args.db else paths().db
    snapshot = Path(args.snapshot) if args.snapshot else _default_snapshot()
    reference = None if args.no_reference else (Path(args.reference) if args.reference else _default_reference())
    checks = validation.validate(db_path, snapshot, reference)
    text = validation.render_report(checks, db_path, snapshot, reference)
    report = validation.write_report(text, paths().reports)
    for c in checks:
        print(f"  [{'ok' if c.ok else c.severity:4s}] {c.name} — {c.detail}")
    failed = [c for c in checks if not c.ok and c.severity == "FAIL"]
    print(f"{'PASS' if not failed else 'FAIL'}: {len(checks)} checks, {len(failed)} failed. Report: {report}")
    if failed:
        sys.exit(1)


def create_database(db_path: Path) -> int:
    """A new, empty database with the whole schema; its schema version."""
    from app import db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        return db.migrate(conn)[-1]
    finally:
        conn.close()


def cmd_init(args) -> None:
    """A new, empty database: for a start without a tracker spreadsheet to import."""
    db_path = Path(args.db) if args.db else paths().db
    if db_path.exists():
        sys.exit(f"{db_path} already exists: nothing done (use `migrate` to upgrade it).")
    version = create_database(db_path)
    print(f"Created an empty database at {db_path} (schema v{version}).")
    print("Next: `uv run career-console boards discover` adds official job boards of Israeli tech companies, "
          "`uv run career-console start` opens the console.")


def cmd_migrate(args) -> None:
    db_path = Path(args.db) if args.db else paths().db
    try:
        result = upgrade.upgrade(db_path, paths().backups)
    except upgrade.UpgradeError as e:
        sys.exit(str(e))
    if result["applied"]:
        print(f"Migrated v{result['from']} -> v{result['to']} (applied {result['applied']}). Backup: {result['backup']}")
    else:
        print(f"Already at v{result['to']}; nothing to migrate.")
    if result["candidates_added"]:
        print(f"Queued {result['candidates_added']} possible duplicate(s) for review (nothing merged).")
    if result["sources_added"]:
        print(f"Added {result['sources_added']} official job board(s) found in your tracker links to the search.")


def cmd_search(args) -> None:
    from search import pipeline
    from app import db
    conn = db.connect(Path(args.db) if args.db else paths().db, create=False)
    try:
        if db.current_version(conn) < db.latest_version():
            sys.exit("Database schema is outdated. Run `career-console migrate` first.")
        try:
            s = pipeline.run_search(conn, trigger="schedule" if args.scheduled else "cli", dry_run=args.dry_run,
                                    force=args.force)
        except pipeline.RunInProgress as e:
            sys.exit(str(e))
    finally:
        conn.close()
    print(f"{'Dry run (nothing saved)' if s['dry_run'] else 'Run ' + str(s['run_id'])}: "
          f"{s.get('sources_ok', 0)} boards checked, {s.get('sources_cached', 0)} recently checked (skipped), "
          f"{s.get('sources_failed', 0)} failed")
    if s.get("sources_cached") and not s.get("sources_ok") and not s.get("sources_failed"):
        print("  every board was checked less than 10 minutes ago; nothing fetched (Verified Jobs unchanged)")
    else:
        print(f"  postings seen {s.get('postings_seen', 0)}, new {s.get('postings_new', 0)}, "
              f"of them in Verified Jobs now {s.get('presented', 0)}")
    print(f"  requests {s['requests']} to free official ATS endpoints; paid calls 0; cost $0.00")
    for company, error in s["errors"]:
        print(f"  ! {company}: {error}")
    if not s["dry_run"]:
        from app import notify
        conn = db.connect(Path(args.db) if args.db else paths().db, create=False)
        try:
            sent = notify.notify_new_jobs(conn)
        finally:
            conn.close()
        if sent["status"] not in ("off", "nothing new"):
            print(f"  notification: {sent['status']} ({sent['items']} new job(s)){' — ' + sent['error'] if sent.get('error') else ''}")
        from search import mail
        db_path = Path(args.db) if args.db else paths().db
        conn = db.connect(db_path, create=False)
        try:
            for r in mail.read(conn, db_path):   # nothing to do without data/mail.env
                print(f"  alert e-mails {r['mailbox']}: " + (f"{r.get('messages', 0)} new e-mails, {r.get('new_jobs', 0)} new jobs"
                                                             if r["ok"] else f"failed — {r['error']}"))
            for f in mail.probe_boards(conn):   # employers named in the alerts, each checked once
                if f.israel_jobs:
                    print(f"  board added: {f.company} ({f.provider}/{f.board}), {f.israel_jobs} job(s) in Israel")
        finally:
            conn.close()


def cmd_notify(args) -> None:
    from app import notify
    cfg = notify.load_config()
    if not cfg:
        sys.exit(f"No {notify.config_path()} yet. Copy examples/notify.env.example there and fill in one channel.")
    if notify.config_path().stat().st_mode & 0o077:
        notify.config_path().chmod(0o600)
        print("  (made notify.env readable only by you)")
    try:
        used = notify.send(cfg, "Career Console: test", "Career Console: notifications work. New verified jobs will arrive here.")
    except notify.NotifyError as e:
        sys.exit(str(e))
    print(f"Test message sent by {used}.")


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _console_pids(port: int) -> list[int]:
    """PIDs listening on the port whose command line is this app's server (never anything else)."""
    import subprocess
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True).stdout
    pids = []
    for pid in {int(p) for p in out.split()}:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        if "career-console" in cmd and "serve" in cmd:
            pids.append(pid)
    return pids


def cmd_start(args) -> None:
    """Start the console in the background (if it is not running) and open it in the browser."""
    import subprocess
    import time
    url = f"http://127.0.0.1:{DEFAULT_PORT}/opportunities"
    if not _port_open(DEFAULT_PORT):
        logs = ensure_private_dir(paths().data / "logs")
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        from app.config import REPO_ROOT
        with open(logs / "server.log", "ab") as log:
            subprocess.Popen([uv, "run", "--project", str(REPO_ROOT), "career-console", "serve"], cwd=REPO_ROOT,
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):
            if _port_open(DEFAULT_PORT):
                break
            time.sleep(0.25)
        else:
            sys.exit(f"The console did not start; see {logs / 'server.log'}")
    if not args.no_browser:
        subprocess.run(["open", url])
    print(f"Career Console is running: {url}")


def cmd_stop(args) -> None:
    import signal
    import time
    pids = _console_pids(DEFAULT_PORT)
    if not pids:
        print("Career Console is not running.")
        return
    for pid in pids:
        os.kill(pid, signal.SIGINT)
    for _ in range(40):
        if not _port_open(DEFAULT_PORT):
            break
        time.sleep(0.25)
    print("Career Console stopped." if not _port_open(DEFAULT_PORT) else "Still stopping; try again in a moment.")


LAUNCH_LABEL = "local.career-console.search"


def schedule_plist(every: int, start: int, end: int) -> tuple[dict, list[int]]:
    from app.config import REPO_ROOT
    hours = list(range(start, end + 1, every)) if every >= 1 and 0 <= start <= end <= 23 else []
    if not hours:
        raise ValueError("Use hours between 0 and 23, e.g. --every 2 --from 8 --to 22")
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    log = str(paths().data / "logs" / "search.log")
    return {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [uv, "run", "--project", str(REPO_ROOT), "career-console", "search", "--scheduled"],
        "WorkingDirectory": str(REPO_ROOT),
        "StartCalendarInterval": [{"Hour": h, "Minute": 0} for h in hours],
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + str(Path(uv).parent)},
    }, hours


def cmd_schedule(args) -> None:
    """Runs `career-console search` on a timetable with macOS launchd (only while the Mac is on)."""
    import plistlib
    import subprocess
    agent = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if args.uninstall:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], capture_output=True)
        agent.unlink(missing_ok=True)
        print("Automatic search removed.")
        return
    try:
        plist, hours = schedule_plist(args.every, args.start, args.end)
    except ValueError as e:
        sys.exit(str(e))
    times = ", ".join(f"{h:02d}:00" for h in hours)
    if not args.install:
        print(f"Plan: search at {times} every day (Mac local time), only while the Mac is on.")
        print(f"To turn it on: uv run career-console schedule --install --every {args.every} --from {args.start} --to {args.end}")
        return
    ensure_private_dir(paths().data / "logs")
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], capture_output=True)  # replace an old one
    result = subprocess.run(["launchctl", "bootstrap", domain, str(agent)], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"launchctl could not load the schedule: {result.stderr.strip() or result.returncode}")
    print(f"Automatic search on: {times} every day while the Mac is on (a missed time runs once after wake).")
    print(f"Log: {plist['StandardOutPath']} · Turn off: uv run career-console schedule --uninstall")


def cmd_desktop(args) -> None:
    from app import desktop
    if args.uninstall:
        removed = desktop.uninstall()
        print(f"Removed {len(removed)} shortcut(s) from the desktop.")
        return
    for app in desktop.install():
        print(f"  {app}")
    print("Desktop shortcuts ready: Career Console (open), Search Jobs Now, Stop Career Console.")


def cmd_feedback(args) -> None:
    from app import db
    from app.config import local_time
    from search import pipeline
    conn = db.connect(Path(args.db) if args.db else paths().db, readonly=True, create=False)
    try:
        f = pipeline.feedback_summary(conn)
    finally:
        conn.close()
    c = f["counts"]
    share = f"{f['good_share']}%" if f["good_share"] is not None else "n/a"
    print(f"Judged {f['judged']}: good finds (applied) {c['applied_now']} ({share}), not for me {c['declined']}, "
          f"duplicates {c['already_applied']}, wrong match {c['skipped']}; "
          f"saved {c['saved']}, maybe {c['maybe']}, hidden {c['hidden']}; marked overqualified {c['overqualified']}")
    for reason, n in f["reasons"]:
        print(f"  wrong match — {reason}: {n}")
    for reason, n in f["mine"]:
        print(f"  not for me — {reason}: {n}")
    for n in f["notes"]:
        print(f"  {local_time(n['created_at_utc'])} [{pipeline.POSTING_DECISIONS.get(n['decision'], n['decision'])}] "
              f"{n['company']} — {n['title']}: {n['note']}")


def cmd_boards(args) -> None:
    from app import db
    from search import discover
    conn = db.connect(Path(args.db) if args.db else paths().db, create=False)
    try:
        results = discover.discover(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    added = [r for r in results if r.israel_jobs and r.note in ("added", "would be added")]
    for r in results:
        where = f"{r.provider}/{r.board} — {r.israel_jobs} of {r.jobs} jobs in Israel" if r.provider else ""
        print(f"  {r.company:22s} {r.note:40s} {where}")
    print(f"{'Would add' if args.dry_run else 'Added'} {len(added)} board(s). Turn any off in Search settings.")


def cmd_backup(args) -> None:
    print(backup_mod.backup(Path(args.db) if args.db else paths().db, paths().backups))


def cmd_restore(args) -> None:
    safety = backup_mod.restore(Path(args.backup), Path(args.db) if args.db else paths().db, paths().backups)
    print(f"Restored {args.backup}." + (f" Previous database saved as {safety}" if safety else ""))


def cmd_serve(args) -> None:
    host = args.host
    if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
        sys.exit("Refusing to listen beyond this computer; LAN access needs a security review first.")
    import uvicorn
    from app.web import create_app
    db_path = Path(args.db) if args.db else paths().db
    if not db_path.exists():   # the first start (from the desktop icon too): an empty database
        create_database(db_path)
        print(f"First start: created an empty database at {db_path}")
    with socket.socket() as probe:
        if probe.connect_ex((host, args.port)) == 0:
            sys.exit(f"Port {args.port} is already in use — Career Console may already be running at "
                     f"http://{host}:{args.port}. Open it, stop the other server, or use --port {args.port + 1}.")
    print(f"Career Console on http://{host}:{args.port}  (Ctrl+C to stop)")
    # Access logs are off: query strings can contain contact names.
    uvicorn.run(create_app(db_path), host=host, port=args.port, access_log=False, log_level="warning")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="career-console", description="Local-first job-search console.")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("import", help="import an XLSX snapshot into a NEW database")
    s.add_argument("--snapshot", required=True)
    s.add_argument("--meta", help="metadata JSON (default: <snapshot>.meta.json next to it)")
    s.add_argument("--db")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("validate", help="check the database against the snapshot and the Phase 2 dry-run")
    s.add_argument("--db")
    s.add_argument("--snapshot")
    s.add_argument("--reference")
    s.add_argument("--no-reference", action="store_true")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("init", help="create a new, empty database (a start without a spreadsheet to import)")
    s.add_argument("--db")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("migrate", help="upgrade the database schema (a verified backup is made first)")
    s.add_argument("--db")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("search", help="check the official job boards now (free; no AI, no paid APIs)")
    s.add_argument("--dry-run", action="store_true", help="fetch and evaluate, but save nothing")
    s.add_argument("--force", action="store_true", help="also re-check boards checked in the last 10 minutes")
    s.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)  # set by the schedule
    s.add_argument("--db")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("notify", help="send a test notification (configure data/notify.env first)")
    s.add_argument("--test", action="store_true", required=True)
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("schedule", help="run the search automatically (macOS); without --install it only shows the plan")
    s.add_argument("--every", type=int, default=2, help="hours between runs (default 2)")
    s.add_argument("--from", dest="start", type=int, default=8, help="first hour (default 8)")
    s.add_argument("--to", dest="end", type=int, default=22, help="last hour (default 22)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("start", help="start the console in the background and open it in the browser")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="stop the console started in the background (or in a terminal)")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("desktop", help="put shortcuts on the macOS desktop: open, search now, stop")
    s.add_argument("--uninstall", action="store_true")
    s.set_defaults(func=cmd_desktop)

    s = sub.add_parser("feedback", help="how the search is doing, from your decisions and notes")
    s.add_argument("--db")
    s.set_defaults(func=cmd_feedback)

    s = sub.add_parser("boards", help="find official boards for known Israeli tech companies (adds those with jobs in Israel)")
    s.add_argument("action", choices=["discover"])
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--db")
    s.set_defaults(func=cmd_boards)

    s = sub.add_parser("backup", help="write a verified backup to data/backups")
    s.add_argument("--db")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="restore a backup (the current database is backed up first)")
    s.add_argument("backup")
    s.add_argument("--db")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("serve", help="run the local web console")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--db")
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
