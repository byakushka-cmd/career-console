"""Apply directly: a likely official posting is suggested next to aggregator links (never replacing them)."""
import pytest

from app import db
from search import official
from tests.test_search_pipeline import run


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


def test_aggregators_are_recognised():
    assert official.is_aggregator("https://il.linkedin.com/jobs/view/123")
    assert official.is_aggregator("https://tsenta.com/jobs/x")
    assert not official.is_aggregator("https://job-boards.greenhouse.io/acme/jobs/1")
    assert not official.is_aggregator("https://www.catonetworks.com/careers/x")


def test_title_similarity_ignores_tracker_notes_after_a_dash():
    assert official.title_similarity("Senior QA Engineer — Payments, Tel Aviv (LinkedIn #1)", "Senior QA Engineer") == 1.0
    assert official.title_similarity("QA Manager", "Backend Developer") == 0.0


def test_finds_the_listed_official_posting_for_the_same_company(conn):
    run(conn)
    index = official.OfficialIndex(conn, ["gotfriends"])
    matches = index.find("Acme Cloud", "Senior QA Engineer — Payments (LinkedIn listing)")
    assert matches and matches[0].posting["title"] == "Senior QA Engineer, Payments"
    assert index.find("Other Company", "Senior QA Engineer") == []      # same title elsewhere: not suggested
    assert index.find("Gotfriends / client", "Senior QA Engineer") == []  # agencies: end client unknown


def test_same_job_id_wins(conn):
    run(conn)
    matches = official.OfficialIndex(conn, []).find("Acme Cloud", "Some other title", ["12345"])
    assert matches and matches[0].reason == "same job ID #12345"


def test_preference_order_employer_then_linkedin_then_job_board():
    assert official.channel("https://job-boards.greenhouse.io/acme/jobs/1") == official.EMPLOYER
    assert official.channel("https://www.catonetworks.com/careers/x") == official.EMPLOYER
    assert official.channel("https://il.linkedin.com/jobs/view/123") == official.LINKEDIN
    assert official.channel("https://notlinkedin.com/jobs/1") == official.EMPLOYER
    assert official.channel("https://www.drushim.co.il/job/1/") == official.JOB_BOARD
    links = ["https://www.drushim.co.il/job/1/", "https://il.linkedin.com/jobs/view/2", "https://www.notch.cx/careers",
             "https://www.comeet.com/jobs/notch/CA.003", "https://il.linkedin.com/jobs/view/2"]
    assert official.ordered_links(links) == [
        "https://www.comeet.com/jobs/notch/CA.003",  # the employer's own job page first
        "https://www.notch.cx/careers",              # then its general careers page
        "https://il.linkedin.com/jobs/view/2",       # then LinkedIn
        "https://www.drushim.co.il/job/1/"]          # a job board last
    assert official.link_label("https://job-boards.greenhouse.io/acme/jobs/1") == "Employer ATS · Greenhouse"
    assert official.link_label("https://www.notch.cx/careers") == "Employer site · notch.cx"
    assert official.link_label("https://www.drushim.co.il/job/1/") == "Job board · drushim.co.il"
    assert official.link_label("https://www.gotfriends.co.il/jobslobby/qa/1/") == "Agency site · gotfriends.co.il"


def test_linkedin_search_is_a_link_you_open_yourself():
    url = official.linkedin_search("Dialog / confidential fintech", "QA Lead — Tel Aviv (req 5)")
    assert url.startswith("https://www.linkedin.com/jobs/search/?")
    assert "keywords=Dialog+QA+Lead&" in url and "geoId=101620260" in url
