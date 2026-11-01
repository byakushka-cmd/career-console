"""Find the job boards of companies the console does not know yet, once a day, from Common Crawl.

Job-board systems have no search across companies, and a search engine needs a paid key. Common Crawl is a free
public archive of the web: its index lists the pages it saved under an address, which names the companies with public
boards. It is updated about once a month and its server is often busy, so a list that could not be read is simply tried
again the next day. Each board found is read once; one with jobs in Israel joins the boards the search reads.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app import db
from search import profile as gates
from search import sources

INDEX = "https://index.commoncrawl.org"
PLACES = ("comeet.com/jobs/", "jobs.lever.co/", "jobs.ashbyhq.com/", "job-boards.greenhouse.io/", "boards.greenhouse.io/")
CRAWLS_TO_READ = 4          # the newest archives, one more for each place per day: each one saved other pages
MAX_PAGES = 10              # of one list
INDEX_TIMEOUT_S = 45
INDEX_SECONDS_PER_DAY = 150
BOARDS_PER_DAY = 150
BOARD_SECONDS_PER_DAY = 180
SLOW_BOARD_S = 25           # a board this slow would hold every search back: it is not added
RECHECK_DAYS = 60           # a company without jobs in Israel may open some later
MAX_BOARDS = 400            # boards in the search, so that it still ends in about a quarter of an hour
MAX_BYTES = 40 * 1024 * 1024
NOT_BOARDS = {"embed", "robots.txt", "favicon.ico", "sitemap.xml", "assets", "static", "jobs"}
_EMBED_FOR = re.compile(r"greenhouse\.io/embed/[a-z_]+\?(?:[^#]*&)?for=([A-Za-z0-9_-]+)", re.I)


def index_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": sources.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=INDEX_TIMEOUT_S) as response:
            return response.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        raise sources.SourceError(f"HTTP {e.code}", e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise sources.SourceError(f"Network error: {getattr(e, 'reason', e)}") from None


def board_key(provider: str, board: str) -> str:
    """One company's board under different addresses: Comeet's page name can change, its ID does not."""
    return board.rsplit("/", 1)[-1].upper() if provider == "comeet" else board.casefold()


def boards_in(lines: bytes) -> set[tuple[str, str]]:
    """The boards named by the addresses of one page of the index."""
    found = set()
    for line in lines.decode("utf-8", "replace").split("\n"):
        try:
            url = json.loads(line)["url"] if line.strip() else ""
        except (ValueError, KeyError, TypeError):
            continue
        embedded = _EMBED_FOR.search(url)
        detected = ("greenhouse", embedded.group(1)) if embedded else sources.detect_board(url)
        if detected and detected[0] in sources.FETCHERS and detected[1].casefold() not in NOT_BOARDS:
            found.add(detected)
    return found


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_place(conn, place: str, crawl: str, get, sleep) -> int:
    """All the boards this archive names under this address. What was read is kept even if a later page fails."""
    base = f"{INDEX}/{crawl}-index?url={place}&matchType=prefix&output=json"
    pages = int(json.loads(get(f"{base}&showNumPages=true"))["pages"])
    boards = 0
    for page in range(min(pages, MAX_PAGES)):
        sleep(1)
        found = boards_in(get(f"{base}&fl=url&page={page}"))
        boards += len(found)
        with db.transaction(conn):
            conn.executemany("""INSERT OR IGNORE INTO crawl_boards(provider, key, board, found_in, found_at_utc)
                                VALUES (?,?,?,?,?)""",
                             [(p, board_key(p, b), b, crawl, _now()) for p, b in sorted(found)])
    with db.transaction(conn):
        conn.execute("INSERT INTO crawl_reads(place, crawl, read_at_utc, boards) VALUES (?,?,?,?)",
                     (place, crawl, _now(), boards))
    return boards


def read_index(conn, get, sleep, clock) -> list[str]:
    """For each address: the newest archive not read yet. -> what could not be read (it is tried again tomorrow)."""
    try:
        crawls = [c["id"] for c in json.loads(get(f"{INDEX}/collinfo.json"))][:CRAWLS_TO_READ]
    except (sources.SourceError, ValueError, KeyError, TypeError) as e:
        return [f"the list of archives: {e}"]
    done = {(r["place"], r["crawl"]) for r in conn.execute("SELECT place, crawl FROM crawl_reads")}
    started, problems = clock(), []
    for place in PLACES:
        for crawl in [c for c in crawls if (place, c) not in done]:
            if clock() - started > INDEX_SECONDS_PER_DAY:
                return problems + ["out of time for today"]
            try:
                _read_place(conn, place, crawl, get, sleep)
                break
            except (sources.SourceError, ValueError, KeyError, TypeError) as e:
                problems.append(f"{place} in {crawl}: {e}")
            sleep(2)
    return problems


def _try_again(error: sources.SourceError) -> bool:
    """The site was busy, which says nothing about the board: it stays first in line for tomorrow."""
    status = getattr(error, "status", None)
    return str(error).startswith("Network error") or status == 429 or (status or 0) >= 500


def check_boards(conn, get, sleep, clock, today: datetime) -> list[str]:
    """Read the boards waiting to be checked, Comeet first: it is the most common system among Israeli companies.
    -> the companies added to the search."""
    from search import pipeline
    known_boards = {(r["provider"], board_key(r["provider"], r["board"])) for r in
                    conn.execute("SELECT provider, board FROM search_sources")}
    known_names = {pipeline.company_name_key(r["company"]) for r in conn.execute("SELECT company FROM search_sources")}
    waiting = conn.execute("""SELECT * FROM crawl_boards WHERE checked_at_utc IS NULL OR checked_at_utc < ?
                              ORDER BY checked_at_utc IS NOT NULL, provider <> 'comeet', found_at_utc, board""",
                           ((today - timedelta(days=RECHECK_DAYS)).isoformat(timespec="seconds"),)).fetchall()
    started, added, checked = clock(), [], 0
    room = MAX_BOARDS - conn.execute("SELECT count(*) FROM search_sources WHERE enabled=1").fetchone()[0]

    def note(row, text, jobs=0, israel=0):
        with db.transaction(conn):
            conn.execute("""UPDATE crawl_boards SET checked_at_utc=?, jobs=?, israel_jobs=?, note=?
                            WHERE provider=? AND key=?""", (_now(), jobs, israel, text, row["provider"], row["key"]))

    for row in waiting:
        if (row["provider"], row["key"]) in known_boards:
            note(row, "already on your list")
            continue
        if len(added) >= room or checked >= BOARDS_PER_DAY or clock() - started > BOARD_SECONDS_PER_DAY:
            break
        checked += 1
        sleep(0.3)
        began = clock()
        try:
            postings = sources.FETCHERS[row["provider"]](row["board"], get)
        except sources.SourceError as e:
            if not _try_again(e):
                note(row, f"not readable: {e}"[:200])
            continue
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            note(row, f"not readable: {e}"[:200])
            continue
        israel = sum(1 for p in postings if gates.geography(p.location, p.countries, p.remote).value == "ISRAEL")
        if not israel:
            note(row, "no jobs in Israel now", len(postings))
        elif clock() - began > SLOW_BOARD_S:
            note(row, "too slow to read at every search", len(postings), israel)
        else:
            company = sources.board_company(row["provider"], row["board"], get)
            if pipeline.company_name_key(company) in known_names:
                note(row, "this company is already read through another board", len(postings), israel)
                continue
            with db.transaction(conn):
                conn.execute("""INSERT OR IGNORE INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                                VALUES (?, ?, ?, 1, 'found: Common Crawl', ?)""",
                             (row["provider"], row["board"], company[:120], _now()))
            known_boards.add((row["provider"], row["key"]))
            known_names.add(pipeline.company_name_key(company))
            note(row, "added", len(postings), israel)
            added.append(company)
    return added


def daily(conn: sqlite3.Connection, day: str, index=index_get, boards=sources.http_get, sleep=time.sleep,
          clock=time.monotonic, today: datetime | None = None) -> list[str] | None:
    """Once a day, before the first search: new boards. -> the companies added; None if already done today."""
    with db.transaction(conn):
        fresh = conn.execute("INSERT OR IGNORE INTO crawl_days(day, started_at_utc) VALUES (?, ?)", (day, _now())).rowcount
    if not fresh:
        return None
    problems = read_index(conn, index, sleep, clock)
    added = check_boards(conn, boards, sleep, clock, today or datetime.now(timezone.utc))
    with db.transaction(conn):
        conn.execute("UPDATE crawl_days SET finished_at_utc=?, added=?, problems=? WHERE day=?",
                     (_now(), len(added), "; ".join(problems)[:500], day))
    return added


def status(conn: sqlite3.Connection) -> dict | None:
    """What the settings page says about it."""
    last = conn.execute("SELECT * FROM crawl_days ORDER BY day DESC LIMIT 1").fetchone()
    if last is None:
        return None
    counts = conn.execute("""SELECT count(*) AS found, sum(checked_at_utc IS NULL) AS waiting,
                             sum(note = 'added') AS added FROM crawl_boards""").fetchone()
    return {"day": last["day"], "problems": last["problems"], "found": counts["found"], "waiting": counts["waiting"] or 0,
            "added": counts["added"] or 0}
