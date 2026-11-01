"""New companies from Common Crawl's public index, once a day. Fake index and fake boards, fictional companies."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from search import crawl, pipeline, sources

DAY = "2026-11-01"
NOW = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
COMEET_PAGE = """<html><script>COMPANY_DATA = {"name": "Vandelay Tech", "location": "Israel"};
COMPANY_POSITIONS_DATA = [%s];
</script></html>"""


def comeet_job(uid, name, country="IL", city="Tel Aviv"):
    return json.dumps({"uid": uid, "name": name, "location": {"name": city, "country": country},
                       "url_comeet_hosted_page": f"https://www.comeet.com/jobs/vandelay/AB.101/{uid}"})


class Index:
    """The answers of the index server, by address."""

    def __init__(self, urls=None, crawls=("CC-MAIN-2026-43", "CC-MAIN-2026-39")):
        self.urls, self.crawls, self.calls, self.down = urls or {}, crawls, [], set()

    def __call__(self, url):
        self.calls.append(url)
        if "collinfo" in url:
            return json.dumps([{"id": c} for c in self.crawls]).encode()
        crawl_id = url.split(".org/")[1].split("-index")[0]
        place = url.split("url=")[1].split("&")[0]
        if (place, crawl_id) in self.down or "all" in self.down:
            raise sources.SourceError("HTTP 504", 504)
        if "showNumPages" in url:
            return json.dumps({"pages": 1}).encode()
        lines = [json.dumps({"url": u}) for u in self.urls.get((place, crawl_id), [])]
        return ("\n".join(lines) + "\n").encode()


class Boards:
    """The boards' own answers, by address."""

    def __init__(self, pages=None):
        self.pages, self.calls = pages or {}, []

    def __call__(self, url, payload=None):
        self.calls.append(url)
        for part, body in self.pages.items():
            if part in url:
                return body.encode()
        raise sources.SourceError("HTTP 404", 404)


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db, create=False)
    yield c
    c.close()


def run(conn, index, boards, day=DAY, clock=None, today=NOW):
    ticks = iter(range(0, 100_000))
    return crawl.daily(conn, day, index, boards, lambda s: None, clock or (lambda: next(ticks) * 0.001), today)


COMEET_CRAWL = ("comeet.com/jobs/", "CC-MAIN-2026-43")


def source(conn, board):
    return conn.execute("SELECT * FROM search_sources WHERE board=?", (board,)).fetchone()


def test_a_board_with_jobs_in_israel_joins_the_search_under_its_own_name(conn):
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/vandelay/AB.101/qa-lead/11.111"]})
    boards = Boards({"vandelay/AB.101": COMEET_PAGE % comeet_job("11.111", "QA Lead")})
    assert run(conn, index, boards) == ["Vandelay Tech"]
    row = source(conn, "vandelay/AB.101")
    assert row["company"] == "Vandelay Tech" and row["provider"] == "comeet" and row["origin"] == "found: Common Crawl"


def test_a_board_without_jobs_in_israel_stays_out_and_is_read_again_only_after_two_months(conn):
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/globex/BB.002"]})
    boards = Boards({"globex/BB.002": COMEET_PAGE % comeet_job("22.222", "QA Lead", "US", "Austin")})
    assert run(conn, index, boards) == []
    assert source(conn, "globex/BB.002") is None
    calls = len(boards.calls)
    assert run(conn, Index(), boards, "2026-11-02", today=NOW + timedelta(days=1)) == []
    assert len(boards.calls) == calls
    conn.execute("UPDATE crawl_boards SET checked_at_utc='2026-08-01T00:00:00+00:00'")
    boards.pages["globex/BB.002"] = COMEET_PAGE % comeet_job("22.222", "QA Lead")
    assert run(conn, Index(), boards, "2026-11-03", today=NOW + timedelta(days=2)) == ["Vandelay Tech"]


def test_a_worldwide_remote_job_is_not_a_job_in_israel(conn):
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/hooli/CC.003"]})
    job = json.dumps({"uid": "33.333", "name": "QA", "location": {"name": "Remote - Worldwide"}, "workplace_type": "remote"})
    assert run(conn, index, Boards({"hooli/CC.003": COMEET_PAGE % job})) == []


def test_only_once_a_day(conn):
    index, boards = Index(), Boards()
    assert run(conn, index, boards) == []
    calls = len(index.calls)
    assert run(conn, index, boards) is None
    assert len(index.calls) == calls
    assert run(conn, index, boards, "2026-11-02") == []
    assert len(index.calls) > calls


def test_each_list_is_read_once_and_the_next_day_goes_to_an_older_archive(conn):
    index = Index()
    run(conn, index, Boards())
    assert not any("CC-MAIN-2026-39" in c for c in index.calls)
    index.calls.clear()
    run(conn, index, Boards(), "2026-11-02")
    assert any("CC-MAIN-2026-39" in c for c in index.calls) and not any("CC-MAIN-2026-43" in c for c in index.calls)
    index.calls.clear()
    run(conn, index, Boards(), "2026-11-03")
    assert [c for c in index.calls if "collinfo" not in c] == []


def test_a_busy_index_falls_back_to_an_older_archive_and_tries_again_tomorrow(conn):
    index = Index({("comeet.com/jobs/", "CC-MAIN-2026-39"): ["https://www.comeet.com/jobs/vandelay/AB.101"]})
    index.down.add(COMEET_CRAWL)
    boards = Boards({"vandelay/AB.101": COMEET_PAGE % comeet_job("11.111", "QA Lead")})
    assert run(conn, index, boards) == ["Vandelay Tech"]
    assert "504" in conn.execute("SELECT problems FROM crawl_days").fetchone()["problems"]
    index.down.clear()
    index.calls.clear()
    run(conn, index, boards, "2026-11-02")
    assert any("CC-MAIN-2026-43" in c and "comeet" in c for c in index.calls)


def test_a_server_that_is_down_harms_nothing(conn):
    index = Index()
    index.down.add("all")
    assert run(conn, index, Boards()) == []
    assert conn.execute("SELECT problems FROM crawl_days").fetchone()["problems"]


def test_a_known_board_is_not_read_even_under_another_page_name(conn):
    conn.execute("""INSERT INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                    VALUES ('comeet', 'oldname/DD.004', 'Initech', 1, 'added by you', ?)""", (NOW.isoformat(),))
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/newname/DD.004/qa/44.444",
                                  "https://www.comeet.com/jobs/newname/dd.004"]})
    boards = Boards()
    assert run(conn, index, boards) == []
    assert boards.calls == []
    assert conn.execute("SELECT count(*) FROM crawl_boards").fetchone()[0] == 1


def test_the_same_company_on_another_system_is_not_read_twice(conn):
    conn.execute("""INSERT INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                    VALUES ('greenhouse', 'vandelaytech', 'Vandelay Tech', 1, 'added by you', ?)""", (NOW.isoformat(),))
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/vandelay/AB.101"]})
    assert run(conn, index, Boards({"vandelay/AB.101": COMEET_PAGE % comeet_job("11.111", "QA")})) == []
    assert source(conn, "vandelay/AB.101") is None


def test_a_slow_board_is_not_added(conn):
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/vandelay/AB.101"]})
    boards = Boards({"vandelay/AB.101": COMEET_PAGE % comeet_job("11.111", "QA")})
    ticks = iter(range(0, 100_000, 30))
    assert run(conn, index, boards, clock=lambda: next(ticks)) == []
    assert "too slow" in conn.execute("SELECT note FROM crawl_boards").fetchone()["note"]


def test_a_daily_limit_leaves_the_rest_for_tomorrow_comeet_first(conn, monkeypatch):
    monkeypatch.setattr(crawl, "BOARDS_PER_DAY", 1)
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/vandelay/AB.101"],
                   ("jobs.lever.co/", "CC-MAIN-2026-43"): ["https://jobs.lever.co/aaa-first/1"]})
    boards = Boards({"vandelay/AB.101": COMEET_PAGE % comeet_job("11.111", "QA")})
    run(conn, index, boards)
    assert [u for u in boards.calls if "comeet" in u] and not [u for u in boards.calls if "lever" in u]
    assert crawl.status(conn)["waiting"] == 1
    run(conn, Index(), boards, "2026-11-02")
    assert crawl.status(conn)["waiting"] == 0


def test_a_busy_board_is_tried_again_tomorrow_and_a_gone_one_is_not(conn):
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/busy/AB.201", "https://www.comeet.com/jobs/gone/AB.202"]})

    class Flaky(Boards):
        def __call__(self, url, payload=None):
            self.calls.append(url)
            raise sources.SourceError("HTTP 504", 504) if "busy" in url else sources.SourceError("HTTP 404", 404)
    boards = Flaky()
    run(conn, index, boards)
    assert crawl.status(conn)["waiting"] == 1
    boards.calls.clear()
    run(conn, Index(), boards, "2026-11-02", today=NOW + timedelta(days=1))
    assert boards.calls == ["https://www.comeet.com/jobs/busy/AB.201"]


def test_no_more_boards_than_the_search_can_read(conn, monkeypatch):
    enabled = conn.execute("SELECT count(*) FROM search_sources WHERE enabled=1").fetchone()[0]
    monkeypatch.setattr(crawl, "MAX_BOARDS", enabled)
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/vandelay/AB.101"]})
    assert run(conn, index, Boards({"vandelay/AB.101": COMEET_PAGE % comeet_job("11.111", "QA")})) == []
    assert crawl.status(conn)["waiting"] == 1


def test_what_is_not_a_board_is_ignored():
    lines = "\n".join([json.dumps({"url": u}) for u in [
        "https://jobs.lever.co/robots.txt", "https://boards.greenhouse.io/embed/job_board?for=acmeco",
        "https://job-boards.greenhouse.io/globex/jobs/123", "https://www.comeet.com/jobs/no-id-here"]])
    broken = lines + '\n{"url": "https://jobs.lever.co/cut\n\nnot json at all\n' + json.dumps({"url": "https://jobs.lever.co/hooli/9 x"})
    assert crawl.boards_in(broken.encode()) == {("greenhouse", "acmeco"), ("greenhouse", "globex"), ("lever", "hooli")}


def test_a_company_name_comes_from_the_board_or_its_address():
    assert sources.board_company("lever", "pied-piper") == "Pied Piper"
    greenhouse = Boards({"boards/acme": json.dumps({"name": "Acme Cloud"})})
    assert sources.board_company("greenhouse", "acme", greenhouse) == "Acme Cloud"
    assert sources.board_company("greenhouse", "umbrella-co", Boards()) == "Umbrella Co"
    assert sources.board_company("comeet", "wonka/EE.005", Boards()) == "Wonka"


def test_the_first_search_of_a_day_reads_the_new_board_right_away(conn):
    index = Index({COMEET_CRAWL: ["https://www.comeet.com/jobs/vandelay/AB.101"]})
    page = Boards({"vandelay/AB.101": COMEET_PAGE % comeet_job("11.111", "QA Lead")})

    def find(c, day):
        return crawl.daily(c, day, index, page, lambda s: None)

    def boards(url, payload=None):
        return page(url, payload)
    summary = pipeline.run_search(conn, get=boards, find_boards=find, now=NOW, sleep=lambda s: None)
    assert summary["new_boards"] == ["Vandelay Tech"]
    assert conn.execute("SELECT count(*) FROM postings WHERE identity LIKE '%11.111%'").fetchone()[0] == 1


def test_a_failure_in_finding_companies_does_not_stop_the_search(conn):
    def broken(c, day):
        with db.transaction(c):
            c.execute("INSERT INTO crawl_days(day, started_at_utc) VALUES (?, ?)", (day, NOW.isoformat()))
        raise RuntimeError("the index changed its format")
    summary = pipeline.run_search(conn, get=Boards(), find_boards=broken, now=NOW, sleep=lambda s: None)
    assert summary["new_boards"] == [] and summary["run_id"]
    assert "format" in conn.execute("SELECT problems FROM crawl_days").fetchone()["problems"]


def test_a_search_with_fake_boards_never_touches_the_index(conn):
    pipeline.run_search(conn, get=Boards(), now=NOW, sleep=lambda s: None)
    assert conn.execute("SELECT count(*) FROM crawl_days").fetchone()[0] == 0


def test_settings_page_says_it_looks_for_companies(imported_db, conn):
    from fastapi.testclient import TestClient
    from app.web import create_app
    client = TestClient(create_app(imported_db, allowed_hosts=("testserver",)))
    assert "Common Crawl" in client.get("/opportunities/settings").text
    run(conn, Index(), Boards())
    assert DAY in client.get("/opportunities/settings").text
