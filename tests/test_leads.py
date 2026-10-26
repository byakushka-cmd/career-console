"""Potential Leads acceptance tests (spec §7), on synthetic data."""
import pytest

from app import db
from search import leads, pipeline

POST = "https://www.linkedin.com/posts/dana-levi_hiring-qa-activity-7251234567890123456-AbCd?utm_source=share"


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


def names(views):
    return {v.lead["role_signal"] for v in views}


def test_israeli_hiring_post_without_ats_is_a_lead_never_a_verified_job(conn):
    leads.capture(conn, POST, author="Dana Levi", author_role="Technical recruiter", company="Acme Cloud",
                  role_signal="Senior QA Automation", job_location="Tel Aviv, hybrid", excerpt="We're hiring a QA lead")
    views = leads.lead_views(conn)
    assert names(views["shown"]) == {"Senior QA Automation"}
    assert pipeline.opportunities(conn)["verified"] == []  # a post never satisfies official verification


def test_us_only_post_by_an_israeli_recruiter_is_hidden_with_its_reason(conn):
    leads.capture(conn, "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000001/",
                  author="Israeli Recruiter", author_role="Recruiter, Tel Aviv", role_signal="QA Manager",
                  job_location="New York, NY (US only)")
    views = leads.lead_views(conn)
    assert names(views["outside"]) == {"QA Manager"} and views["shown"] == []
    assert "new york" in views["outside"][0].geo.reason   # the author's Tel Aviv location is not evidence


def test_worldwide_remote_open_to_israel_is_shown(conn):
    leads.capture(conn, "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000002/",
                  role_signal="Remote SDET", job_location="Remote - worldwide")
    assert names(leads.lead_views(conn)["shown"]) == {"Remote SDET"}


def test_unspecified_location_goes_to_geo_review_until_you_confirm(conn):
    lead_id, _ = leads.capture(conn, "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000003/",
                               role_signal="QA Engineer", excerpt="DM me if interested")
    views = leads.lead_views(conn)
    assert names(views["geo_review"]) == {"QA Engineer"} and views["shown"] == []
    leads.record_decision(conn, lead_id, "confirm_israel", "Asked: the role is in Herzliya")
    assert names(leads.lead_views(conn)["shown"]) == {"QA Engineer"}


def test_same_post_twice_merges_and_keeps_both_observations(conn):
    first, created1 = leads.capture(conn, POST, role_signal="QA lead", job_location="Tel Aviv")
    second, created2 = leads.capture(conn, POST.replace("?utm_source=share", ""), excerpt="Second look: still open",
                                     author="Dana Levi")
    assert first == second and (created1, created2) == (True, False)
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM lead_observations WHERE lead_id=?", (first,)).fetchone()[0] == 2
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (first,)).fetchone()
    assert lead["author"] == "Dana Levi" and lead["job_location"] == "Tel Aviv"  # gaps filled, first capture kept


def test_decisions_hide_and_restore_leads(conn):
    lead_id, _ = leads.capture(conn, POST, role_signal="QA lead", job_location="Haifa")
    leads.record_decision(conn, lead_id, "skipped", "Not QA really")
    assert names(leads.lead_views(conn)["hidden"]) == {"QA lead"}
    leads.record_decision(conn, lead_id, "restored")
    assert names(leads.lead_views(conn)["shown"]) == {"QA lead"}
    with pytest.raises(ValueError):
        leads.record_decision(conn, lead_id, "send_message")  # nothing is ever sent


@pytest.mark.parametrize("bad", [{"post_url": "not a link"}, {"post_url": POST, "posted_on": "yesterday"},
                                 {"post_url": POST, "author_url": "javascript:alert(1)"}])
def test_capture_validation(conn, bad):
    url = bad.pop("post_url")
    with pytest.raises(ValueError):
        leads.capture(conn, url, **bad)


def test_search_links_are_plain_linkedin_searches_tied_to_israel():
    import re
    from urllib.parse import unquote
    links = leads.search_links()
    assert all(l["url"].startswith("https://www.linkedin.com/") for l in links)
    for link in links:   # LinkedIn's Israel filter, Hebrew words, or Israeli cities: never the whole world
        url = unquote(link["url"])
        assert "101620260" in url or re.search("[א-ת]", url) or "Tel Aviv" in url, link["label"]
    assert links[0]["label"].startswith("Jobs in Israel: QA lead")   # your level first


def test_possible_contacts_are_searches_you_open_yourself():
    links = leads.contact_links("Dialog / confidential fintech")
    assert [l["label"] for l in links] == ["Recruiters", "QA leaders", "People you know there"]
    assert all(l["url"].startswith("https://www.linkedin.com/search/results/people/?keywords=%22Dialog%22") for l in links)
    assert "geoUrn=" in links[0]["url"] and "network=%5B%22F%22%2C%22S%22%5D" in links[2]["url"]
    assert leads.contact_links("") == []
