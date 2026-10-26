import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.legacy_import import file_sha256
from app.web import create_app


@pytest.fixture
def client(imported_db):
    return TestClient(create_app(imported_db, allowed_hosts=("testserver",)))


def refs(html):
    return re.findall(r'class="rec-ref mono"[^>]*>([^<]+)<', html)


def test_three_views_render_with_mobile_navigation(client):
    for url in ("/pipeline", "/opportunities", "/opportunities?tab=leads", "/diagnostics"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert 'name="viewport"' in r.text and 'class="nav-bottom"' in r.text
        for label in ("New Opportunities", "My Pipeline", "Diagnostics"):
            assert label in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/pipeline"


def test_pipeline_lists_every_record_including_closed(client):
    html = client.get("/pipeline").text
    assert len(refs(html)) == 12  # 9 tracker rows + 3 Comeet rows (header and empty rows excluded)
    assert "Rejected" in html and "Initech" in html


@pytest.mark.parametrize("query, expected", [
    ("acme", {"MT 2", "MT 3", "CI 2"}),
    ("dana levi", {"MT 2"}),          # recruiter / contact column
    ("12345", {"MT 2", "MT 3", "CI 2"}),  # requisition ID
    ("globex senior", {"MT 4", "MT 11", "CI 4"}),
])
def test_search_by_company_contact_and_req_id(client, query, expected):
    assert set(refs(client.get("/pipeline", params={"q": query}).text)) == expected


def test_status_groups_source_and_review_filters(client):
    closed = client.get("/pipeline?group=closed").text
    # the Comeet index rows were last checked on Sep 27–28: not applied to for over two weeks, so expired
    assert set(refs(closed)) == {"MT 3", "MT 5", "MT 6", "MT 11", "CI 2", "CI 3", "CI 4"} and "Expired" in closed
    assert set(refs(client.get("/pipeline?group=decide").text)) == {"MT 9", "MT 10"}
    assert set(refs(client.get("/pipeline?source=comeet").text)) == {"CI 2", "CI 3", "CI 4"}
    # rows with open items that need a decision; INFO notes and auto-linked groups do not count
    assert set(refs(client.get("/pipeline?review=1").text)) == {"MT 2", "MT 4", "MT 5", "MT 10", "CI 2", "CI 4"}
    assert client.get("/pipeline?group=nonsense").status_code == 200


def test_headline_tiles_and_mix_bar(client):
    html = client.get("/pipeline").text
    for label in ("New discoveries", "Not applied yet", "On hold", "In progress", "Closed", "Pipeline mix"):
        assert label in html
    assert "Potential leads (" not in html          # no leads captured: no link
    assert 'class="seg pr"' in html and "<title>In progress · 2 (17%)</title>" in html


def test_record_shows_original_text_history_and_provenance(client, imported_db):
    html = client.get("/pipeline", params={"q": "dana"}).text
    record_id = re.search(r'href="/records/(\d+)', html).group(1)
    page = client.get(f"/records/{record_id}").text
    assert "Applied / confirmation received" in page   # original status text
    assert "Dana Levi — recruiter" in page             # contact kept
    assert "Linked observations" in page and "MT 3" in page and "CI 2" in page
    assert "Row SHA-256" in page and "America/Los_Angeles" in page
    assert 'rel="noopener noreferrer"' in page


def test_malformed_row_is_shown_verbatim_with_a_warning(client):
    html = client.get("/pipeline", params={"q": "initech"}).text
    record_id = re.search(r'href="/records/(\d+)', html).group(1)
    page = client.get(f"/records/{record_id}").text
    assert "No — absent from official careers listing" in page
    assert "Expected a date here" in page and "Invalid last update" in page


def test_unknown_record_is_404(client):
    assert client.get("/records/999999").status_code == 404


def test_opportunity_tabs_are_separate_and_never_show_unverified_jobs(client):
    verified = client.get("/opportunities?tab=verified").text
    leads = client.get("/opportunities?tab=leads").text
    for page in (verified, leads):
        nav = re.search(r'<nav class="tabs"[^>]*>(.*?)</nav>', page, re.S).group(1)
        assert 'href="/opportunities?tab=verified"' in nav and 'href="/opportunities?tab=leads"' in nav
        assert nav.count("aria-current") == 1
    assert "Run the first search" in verified and "Globex" not in verified  # a tracker row is not a verified job
    assert "No leads yet" in leads and "Find leads on LinkedIn" in leads and "Add a lead" in leads


def test_diagnostics_shows_health_queue_and_groups(client):
    html = client.get("/diagnostics").text
    assert "SQLite integrity check" in html and ">ok<" in html.replace("<i></i>", "")
    assert "Invalid last update" in html and "Application history reconciliation" in html
    assert "Needs your decision" in html and "Same requisition?" in html and "Auto-linked requisition" in html
    assert "Already applied" in html  # screening rules shown verbatim


def test_security_headers_and_private_caching(client):
    r = client.get("/pipeline")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert ' style="' not in r.text  # inline styles would be blocked by the CSP


def test_unexpected_host_is_rejected(imported_db):
    c = TestClient(create_app(imported_db, allowed_hosts=("127.0.0.1",)), base_url="http://evil.example")
    assert c.get("/pipeline").status_code == 400


def test_browsing_is_read_only(client, imported_db):
    before = file_sha256(imported_db)
    for url in ("/pipeline", "/diagnostics", "/opportunities", "/pipeline?q=acme"):
        client.get(url)
    assert client.post("/pipeline").status_code == 405
    assert file_sha256(imported_db) == before


def test_missing_database_fails_fast(tmp_path):
    with pytest.raises(SystemExit):
        create_app(tmp_path / "missing.sqlite")


def first_open_issue_id(client, title="Invalid last update"):
    html = client.get("/diagnostics").text
    return int(re.search(r'id="item-migration_issue-(\d+)">\s*<div class="ritem-head">\s*<span class="tag hu">ERROR</span>\s*<h3>' + title, html).group(1))


def test_decision_form_records_an_event_and_resolves_the_item(client):
    issue_id = first_open_issue_id(client)
    r = client.post(f"/review/migration_issue/{issue_id}", headers={"Origin": "http://testserver"},
                    data={"decision": "acknowledged", "note": "checked", "back": "/diagnostics"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/diagnostics?open=done#item-migration_issue-{issue_id}"
    html = client.get("/diagnostics").text
    assert "Keep as is" in html and "checked" in html and "1 decision(s) logged" in html


def test_resolving_a_candidate_clears_its_review_tag(client):
    html = client.get("/diagnostics").text
    dup_id = re.search(r'id="item-duplicate_review-(\d+)">\s*<div class="ritem-head">\s*<span class="tag hu">CANDIDATE', html).group(1)
    client.post(f"/review/duplicate_review/{dup_id}", headers={"Origin": "http://testserver"},
                data={"decision": "different_requisitions"})
    assert "CI 4" not in refs(client.get("/pipeline?review=1").text)


@pytest.mark.parametrize("headers", [
    {},                                                    # no origin information at all
    {"Origin": "http://evil.example"},
    {"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
])
def test_cross_site_posts_are_refused(client, headers):
    r = client.post("/review/migration_issue/1", headers=headers, data={"decision": "acknowledged"})
    assert r.status_code == 403
    assert "0 decision(s) logged" in client.get("/diagnostics").text


def test_bad_decision_is_400_and_open_redirect_is_blocked(client):
    same = {"Origin": "http://testserver"}
    assert client.post("/review/migration_issue/1", headers=same, data={"decision": "nonsense"}).status_code == 400
    r = client.post("/review/migration_issue/1", headers=same, follow_redirects=False,
                    data={"decision": "acknowledged", "back": "//evil.example/x"})
    assert r.headers["location"] == "/diagnostics?open=done#item-migration_issue-1"


def test_app_refuses_an_outdated_schema(imported_db):
    conn = db.connect(imported_db)
    conn.execute("DELETE FROM schema_version WHERE version = (SELECT max(version) FROM schema_version)")
    conn.close()
    with pytest.raises(SystemExit, match="career-console migrate"):
        create_app(imported_db)


def test_recorded_times_are_shown_in_israel_time():
    from app.web import local_time
    assert local_time("2026-10-14T12:21:00+00:00") == "2026-10-14 15:21"  # IDT, UTC+3
    assert local_time("2026-12-01T12:00:00+00:00") == "2026-12-01 14:00"  # IST, UTC+2
    assert local_time(None) == ""



def record_id_for(client, ref):
    html = client.get("/pipeline").text
    return re.search(r'id="rec-(\d+)" href="/records/\d+[^"]*">(?:(?!</a>).)*?>' + ref + r'<', html, re.S).group(1)


def test_updating_a_tracker_status_moves_it_and_keeps_the_original(client):
    rid = record_id_for(client, "MT 9")                      # Stark Labs, on hold in the tracker
    back = "/pipeline?group=decide"
    r = client.post(f"/records/{rid}/status", headers={"Origin": "http://testserver"}, follow_redirects=False,
                    data={"state": "CLOSED", "note": "404 on the official site", "back": back})
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith(f"{back}&updated={rid}&prev=TRACKER#")
    assert "MT 9" not in refs(client.get(back).text)        # left "On hold"
    page = client.get(location).text
    assert "Undo" in page and "Gone / stale" in page
    detail = client.get(f"/records/{rid}").text
    assert "set by you" in detail and "404 on the official site" in detail
    assert "HOLD — not listed on official employer careers; NOT APPLIED" in detail   # tracker text unchanged
    client.post(f"/records/{rid}/status", headers={"Origin": "http://testserver"}, data={"state": "TRACKER", "back": back})
    assert "MT 9" in refs(client.get(back).text)            # undo: back to the tracker's own reading


def test_status_update_validation(client):
    rid = record_id_for(client, "MT 9")
    same = {"Origin": "http://testserver"}
    assert client.post(f"/records/{rid}/status", headers=same, data={"state": "DELETED"}).status_code == 400
    assert client.post("/records/999999/status", headers=same, data={"state": "CLOSED"}).status_code == 404
    assert client.post(f"/records/{rid}/status", headers={"Origin": "http://evil.example"}, data={"state": "CLOSED"}).status_code == 403
    r = client.post(f"/records/{rid}/status", headers=same, follow_redirects=False,
                    data={"state": "CLOSED", "back": "//evil.example/pipeline"})
    assert r.headers["location"].startswith("/pipeline?updated=")


def test_record_links_put_the_employer_first():
    from app import web
    values = [None] * 17
    values[0], values[1] = "Notch", "AI QA Engineer"
    values[14] = "https://il.linkedin.com/jobs/view/ai-qa-engineer-at-notch-44"
    values[15] = "LinkedIn Apply; employer: https://www.notch.cx/careers and https://www.comeet.com/jobs/notch/CA.003."
    rec = web.Record(1, web.MASTER, 202, values, "DISCOVERED", None, 0, 0)
    assert rec.url == "https://www.comeet.com/jobs/notch/CA.003"
    assert rec.links == ["https://www.comeet.com/jobs/notch/CA.003", "https://www.notch.cx/careers",
                         "https://il.linkedin.com/jobs/view/ai-qa-engineer-at-notch-44"]


def test_static_files_are_versioned_so_new_pages_never_get_an_old_stylesheet(client):
    html = client.get("/pipeline").text
    css = re.search(r'href="(/static/app\.css\?v=[0-9a-f]{10})"', html)
    assert css and re.search(r'src="/static/forms\.js\?v=[0-9a-f]{10}" defer', html)
    r = client.get(css.group(1))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_status_note_sits_below_the_buttons_and_grows(client):
    html = client.get("/records/5").text
    form = re.search(r'<form class="decide".*?</form>', html, re.S).group(0)
    assert form.index('class="choices"') < form.index("<textarea") and 'rows="1"' in form


def test_your_links_are_saved_privately_and_copied_from_every_page(client, imported_db):
    from app import my_links
    assert 'data-copy=' not in client.get("/pipeline").text                 # nothing saved: no button
    r = client.post("/opportunities/settings/links", headers={"Origin": "http://testserver"}, follow_redirects=False,
                    data={"linkedin": "https://www.linkedin.com/in/test-person/?isSelfProfile=true", "other": ""})
    assert r.status_code == 303 and r.headers["location"].endswith("saved=links#links")
    saved = my_links.path_for(imported_db)
    assert my_links.load(saved) == {"linkedin": "https://www.linkedin.com/in/test-person/"}
    assert oct(saved.stat().st_mode)[-3:] == "600"
    page = client.get("/opportunities").text
    assert 'data-copy="https://www.linkedin.com/in/test-person/"' in page and "<span>LinkedIn</span>" in page
    bad = client.post("/opportunities/settings/links", headers={"Origin": "http://testserver"},
                      data={"linkedin": "https://example.com/me", "other": ""})
    assert bad.status_code == 400 and "linkedin.com/in/" in bad.text


def test_lead_roles_and_engineer_roles_are_kept_apart(client):
    """Your first priority is lead roles: My Pipeline shows them apart from engineer roles, with a mark."""
    from app import web
    leads, engineers = refs(client.get("/pipeline?level=lead").text), refs(client.get("/pipeline?level=engineer").text)
    assert leads and engineers and not set(leads) & set(engineers)
    assert set(leads) | set(engineers) == set(refs(client.get("/pipeline").text))
    page = client.get("/pipeline?level=lead&group=closed").text
    assert 'aria-current="true">All levels' not in page and "Lead roles" in page
    assert set(refs(page)) <= set(leads) and 'title="Lead, manager or head role' in page
    assert web.LEVELS == {"lead": "Lead roles", "engineer": "Engineer & other"}


def test_an_added_record_keeps_the_links_its_card_had(client, imported_db):
    """From an alert to My Pipeline: the employer's site search and the same job from other alerts stay with it."""
    from app import added
    conn = db.connect(imported_db)
    for identity, url, place in (("linkedin:4470000101", "https://www.linkedin.com/jobs/view/4470000101/", "Tel Aviv"),
                                 ("linkedin:4470000102", "https://www.linkedin.com/jobs/view/4470000102/", "Haifa")):
        conn.execute("""INSERT INTO mail_jobs(identity, source, title, company, location, url, first_seen_at_utc, last_seen_at_utc)
                        VALUES (?, 'LinkedIn', 'QA Team Lead', 'Wonka Labs', ?, ?, '2026-10-20T08:00:00+00:00',
                                '2026-10-20T08:00:00+00:00')""", (identity, place, url))
    added_id = added.add(conn, "https://www.linkedin.com/jobs/view/4470000101/", "Wonka Labs — QA Team Lead", "DISCOVERED",
                         company="Wonka Labs", role="QA Team Lead")
    conn.close()
    page = client.get(f"/added/{added_id}").text
    assert "Find on Wonka Labs's site" in page and "also: Haifa" in page and "linkedin.com/jobs/view/4470000102" in page
