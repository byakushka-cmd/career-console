import json

import pytest

from app import db
from search import discover, sources


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


def fake_get(url):
    if "boards-api.greenhouse.io/v1/boards/initech/" in url:
        return json.dumps({"jobs": [{"id": 1, "title": "QA", "location": {"name": "Tel Aviv, Israel"},
                                     "absolute_url": "https://boards.greenhouse.io/initech/jobs/1"}]}).encode()
    if "api.lever.co/v0/postings/namesake" in url:  # a namesake abroad: board exists, no Israel jobs
        return json.dumps([{"id": "x", "text": "Sales", "categories": {"location": "Denver, CO"},
                            "hostedUrl": "https://jobs.lever.co/namesake/x"}]).encode()
    raise sources.SourceError("HTTP 404", 404)


def test_boards_are_added_only_with_jobs_in_israel(conn):
    results = discover.discover(conn, {"Initech": [], "Namesake": [], "Nobody": []}, get=fake_get, sleep=lambda s: None)
    by = {r.company: r for r in results}
    assert (by["Initech"].provider, by["Initech"].board, by["Initech"].note) == ("greenhouse", "initech", "added")
    assert by["Namesake"].israel_jobs == 0 and "no jobs in Israel" in by["Namesake"].note
    assert "no public" in by["Nobody"].note
    rows = {(r["provider"], r["board"]) for r in conn.execute("SELECT * FROM search_sources WHERE origin LIKE 'found%'")}
    assert rows == {("greenhouse", "initech")}


def test_dry_run_adds_nothing_and_known_companies_are_skipped(conn):
    before = conn.execute("SELECT count(*) FROM search_sources").fetchone()[0]
    results = discover.discover(conn, {"Initech": [], "Acme Cloud": []}, get=fake_get, sleep=lambda s: None, dry_run=True)
    assert {r.company: r.note for r in results} == {"Initech": "would be added", "Acme Cloud": "already on your list"}
    assert conn.execute("SELECT count(*) FROM search_sources").fetchone()[0] == before


def test_board_name_variants():
    assert discover.board_names("monday.com", ["mondaycom", "monday"]) == ["mondaycom", "monday", "monday-com"]
    assert discover.board_names("Run:ai", ["runai"]) == ["runai", "run-ai"]


def test_known_comeet_boards_are_added_only_with_jobs_in_israel(conn):
    def comeet_get(url):
        jobs = {"https://www.comeet.com/jobs/globex2/AB.123": [{"uid": "11.111", "name": "QA Engineer",
                                                               "location": {"name": "Haifa", "country": "IL"}}],
                "https://www.comeet.com/jobs/abroad/CD.456": [{"uid": "22.222", "name": "Sales",
                                                              "location": {"name": "Berlin", "country": "DE"}}]}
        if url not in jobs:
            raise sources.SourceError("HTTP 404", 404)
        return ("<script>\nvar COMPANY_POSITIONS_DATA = " + json.dumps(jobs[url]) + ";\n</script>").encode()
    results = discover.discover(conn, {}, get=comeet_get, sleep=lambda s: None,
                                comeet={"Globex Two": "globex2/AB.123", "Abroad Ltd": "abroad/CD.456",
                                        "Gone": "gone/EF.789", "Acme Cloud": "acme/AA.001"})
    by = {r.company: r for r in results}
    assert (by["Globex Two"].provider, by["Globex Two"].board, by["Globex Two"].note) == ("comeet", "globex2/AB.123", "added")
    assert "no jobs in Israel" in by["Abroad Ltd"].note and "not readable" in by["Gone"].note
    assert by["Acme Cloud"].note == "already on your list"
    assert ("comeet", "globex2/AB.123") in {(r["provider"], r["board"]) for r in conn.execute("SELECT * FROM search_sources")}


def test_known_comeet_boards_are_well_formed():
    for company, board in discover.COMEET_BOARDS.items():
        assert sources.COMEET_BOARD.fullmatch(board), company
