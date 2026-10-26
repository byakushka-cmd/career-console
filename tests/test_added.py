"""Quick add: a link and a few words (pasted or dictated) become a record in My Pipeline."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import added, db
from app.web import create_app
from search import pipeline
from search.profile import DEFAULT_PROFILE

SAME = {"Origin": "http://testserver"}
CARD = """Company logo for, Acme Cloud Systems Ltd.
Acme Cloud Systems Ltd


8073 מוביל/ה טכנולוגי/ת QA & Automation + AI"""


def test_a_pasted_linkedin_card_gives_company_role_and_job_id():
    found = added.parse("https://www.linkedin.com/jobs/view/4479000000/", CARD, DEFAULT_PROFILE)
    assert found["company"] == "Acme Cloud Systems Ltd"
    assert found["role"] == "מוביל/ה טכנולוגי/ת QA & Automation + AI" and found["job_ids"] == ["8073"]


@pytest.mark.parametrize("url, company", [
    ("https://eprfireworks.zohorecruit.com/jobs/Careers/825427000002383049/QA-Engineer?source=CareerSite", "Eprfireworks"),
    ("https://boards.greenhouse.io/acme-cloud/jobs/123456", "Acme Cloud"),
    ("https://careers.spotower.com/qa-lead", "Spotower"),
    ("https://www.linkedin.com/jobs/view/4479000000/", ""),          # LinkedIn names nobody
    ("https://www.drushim.co.il/job/38571808/fbc345c5/", ""),         # nor does a job board
])
def test_the_employer_is_read_from_its_own_job_site(url, company):
    assert added.company_from_url(url) == company


def test_labelled_and_plain_texts():
    alljobs = "מהנדס/ת בדיקות תוכנה\nחברה: ALLSTARSIT\nמקום העבודה: ירושלים"
    found = added.parse(None, alljobs, DEFAULT_PROFILE)
    assert found["company"] == "ALLSTARSIT" and found["role"] == "מהנדס/ת בדיקות תוכנה" and found["location"] == "ירושלים"
    plain = added.parse(None, "Tikal\nQA Team Lead (Req 94512)\nTel Aviv", DEFAULT_PROFILE)
    assert plain["company"] == "Tikal" and plain["role"] == "QA Team Lead (Req 94512)" and plain["job_ids"] == ["94512"]
    assert plain["location"] == "Tel Aviv"


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


def test_add_set_status_and_correct(conn):
    new = added.add(conn, "https://www.linkedin.com/jobs/view/4479000000/", CARD, "APPLIED")
    [a] = added.load(conn)
    assert (a.id, a.state, a.company, a.job_ids) == (new, "APPLIED", "Acme Cloud Systems Ltd", ["8073"])
    assert a.applied == a.moved
    added.set_status(conn, new, "INTERVIEW", "call with the recruiter on Sunday")
    added.edit(conn, new, company="Acme (Cloud unit)", role=a.role, url=a.url, job_ids="8073", location="Tel Aviv")
    [a] = added.load(conn)
    assert a.state == "INTERVIEW" and a.note == "call with the recruiter on Sunday" and a.company == "Acme (Cloud unit)"
    assert [e["decision"] for e in a.history] == ["APPLIED", "INTERVIEW", "edited"]
    assert "Acme Cloud Systems Ltd" in a.history[-1]["note"]          # the old value is kept


def test_what_is_missing_is_asked_for(conn):
    with pytest.raises(ValueError, match="Paste a link"):
        added.add(conn, "", "", "APPLIED")
    with pytest.raises(ValueError, match="Company field"):
        added.add(conn, "https://www.linkedin.com/jobs/view/4479000000/", "", "APPLIED")
    assert added.add(conn, "", "", "DISCOVERED", company="Tikal", role="QA Team Lead") > 0


def test_interested_records_expire_like_the_tracker(conn):
    added.add(conn, "", "Tikal\nQA Team Lead", "DISCOVERED")
    [a] = added.load(conn, on=date(2026, 12, 31))
    assert a.state == "EXPIRED" and a.status == "DISCOVERED"


def test_the_search_knows_what_you_added(conn):
    added.add(conn, "", "Acme Cloud\nSenior QA Engineer, Payments", "APPLIED")
    history = pipeline.history_index(conn)
    verdict, hits, _ = history.classify("greenhouse:999", "Acme Cloud", "Senior QA Engineer, Payments", None)
    assert verdict == "POSSIBLE_HISTORY" and hits[0].ref.startswith("AD ") and hits[0].href.startswith("/added/")


@pytest.fixture
def client(imported_db):
    return TestClient(create_app(imported_db, allowed_hosts=("testserver",)))


def test_quick_add_from_the_main_page(client):
    home = client.get("/pipeline").text
    assert 'action="/added"' in home and "Add an application" in home and "data-dictate" in home
    r = client.post("/added", headers=SAME, follow_redirects=False,
                    data={"url": "https://www.linkedin.com/jobs/view/4479000000/", "text": CARD, "state": "APPLIED"})
    assert r.status_code == 303 and re.fullmatch(r"/added/\d+\?new=1", r.headers["location"])
    page = client.get(r.headers["location"]).text
    assert "Added to My Pipeline" in page and "Acme Cloud Systems Ltd" in page and "#8073" in page
    listed = client.get("/pipeline?source=added").text
    assert "Acme Cloud Systems Ltd" in listed and re.search(r'class="rec-ref mono"[^>]*>AD \d+<', listed)
    added_id = r.headers["location"].split("/")[2].split("?")[0]
    s = client.post(f"/added/{added_id}/status", headers=SAME, data={"state": "REJECTED", "note": "mail from the recruiter"},
                    follow_redirects=False)
    assert s.status_code == 303
    assert "Rejected by them" in client.get(f"/added/{added_id}").text


def test_quick_add_keeps_what_you_wrote_when_something_is_missing(client):
    r = client.post("/added", headers=SAME, data={"url": "https://www.linkedin.com/jobs/view/4479000000/",
                                                  "text": "", "state": "APPLIED"})
    assert r.status_code == 400 and "Company field" in r.text and "4479000000" in r.text
