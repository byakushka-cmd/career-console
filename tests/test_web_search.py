"""Sniffer UI: run, list, decisions, settings, sources, run detail — against fake official boards."""
import re

import pytest
from fastapi.testclient import TestClient

from app.web import create_app
from tests.test_search_pipeline import FakeBoards

SAME = {"Origin": "http://testserver"}


@pytest.fixture
def boards():
    return FakeBoards()


@pytest.fixture
def client(imported_db, boards, monkeypatch):
    monkeypatch.setattr("search.pipeline.POLITE_DELAY_S", 0)
    app = create_app(imported_db, allowed_hosts=("testserver",), search_get=boards)
    c = TestClient(app)
    c.app_ref = app
    return c


def run_now(client):
    r = client.post("/search/run", headers=SAME, follow_redirects=False)
    assert r.status_code == 303
    client.app_ref.state.search_thread.join(timeout=10)
    return r


def posting_ids(html):
    return re.findall(r'<article class="job" id="posting-(\d+)">', html)


def test_run_from_the_web_fills_verified_jobs(client, boards):
    assert "Run the first search" in client.get("/opportunities").text
    assert run_now(client).headers["location"] == "/opportunities?tab=verified&run=started#search"
    home = client.get("/pipeline").text
    assert "Run search now" in home and "Last search" in home              # on the main page too
    assert "Run search now" in client.get("/opportunities").text
    html = client.get("/opportunities").text
    assert len(posting_ids(html)) == 2 and len(boards.calls) == 3
    assert "Senior QA Engineer, Payments" in html and "Apply · Acme Cloud&#39;s own form (Greenhouse)" in html
    assert "Israel · Tel Aviv-Yafo, Israel" in html                      # where it is, at a glance
    assert "Web and API testing" in html and "Full description" in html     # what the role is, briefly
    assert "Updated" in html                                              # Comeet: only its last update date
    assert "Older than 14 days (1)" in html                               # published in July: kept, not shown
    assert not re.search(r'class="title-link"[^>]*>QA Automation Lead \(Req 12345\)<', html)  # applied before: never new
    assert "Earlier here" in html and "rejected here before" in html     # …but shown as history at that company
    assert "Needs geo review (1)" in html
    older = client.get("/opportunities?tab=verified&show=older").text
    assert "Quality Engineer" in older and "Older than 14 days" in older and "Back to new jobs" in older
    tiles = client.get("/pipeline").text
    assert re.search(r"New discoveries</span>\s*<b>2</b>", tiles)


def test_after_a_decision_you_stay_in_the_list_with_undo(client):
    run_now(client)
    first, second = posting_ids(client.get("/opportunities").text)[:2]
    r = client.post(f"/postings/{first}/decision", headers=SAME, data={"decision": "hidden", "view": "verified"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/opportunities?tab=verified&done={first}#posting-{second}"  # next card, same list
    page = client.get(r.headers["location"]).text
    assert first not in posting_ids(page) and "Undo" in page and 'class="callout info notice"' in page
    hidden = client.get("/opportunities?show=hidden").text
    assert first in posting_ids(hidden) and "Restore" in hidden
    client.post(f"/postings/{first}/decision", headers=SAME, data={"decision": "restored", "view": "hidden"})
    assert first in posting_ids(client.get("/opportunities").text)


def test_good_find_applied_moves_the_job_to_my_pipeline(client):
    run_now(client)
    page = client.get("/opportunities").text
    pid = re.search(r'<a class="title-link" href="/postings/(\d+)">Senior QA Engineer, Payments<', page).group(1)
    client.post(f"/postings/{pid}/decision", headers=SAME, data={"decision": "applied_now", "view": "verified"})
    pipeline = client.get("/pipeline?source=console").text
    assert "Senior QA Engineer, Payments" in pipeline and f"CC {pid}" in pipeline and f'href="/postings/{pid}"' in pipeline
    assert "Applied" in pipeline
    detail = client.get(f"/postings/{pid}").text
    assert "Good find — applied" in detail and "In My Pipeline" in detail
    client.post(f"/postings/{pid}/decision", headers=SAME, data={"decision": "restored", "view": "posting"})
    assert "Senior QA Engineer, Payments" not in client.get("/pipeline?source=console").text
    assert client.get("/postings/999999").status_code == 404


def test_not_a_fit_keeps_the_reason(client):
    run_now(client)
    pid = posting_ids(client.get("/opportunities").text)[0]
    client.post(f"/postings/{pid}/decision", headers=SAME,
                data={"decision": "skipped", "note": "Regulatory / medical device", "view": "verified"})
    hidden = client.get("/opportunities?show=hidden").text
    assert "Wrong match" in hidden and "Regulatory / medical device" in hidden


def test_settings_save_and_validation(client):
    page = client.get("/opportunities/settings").text
    assert "Search terms" in page and "Official job boards" in page and "acme" in page
    bad = client.post("/opportunities/settings/profile", headers=SAME,
                      data={"include_terms": "", "exclude_terms": "", "low_priority_terms": "", "fresh_days": "7", "max_age_days": "14"})
    assert bad.status_code == 400 and "At least one search term" in bad.text
    ok = client.post("/opportunities/settings/profile", headers=SAME, follow_redirects=False,
                     data={"include_terms": "qa\nsdet", "exclude_terms": "embedded", "low_priority_terms": "junior",
                           "fresh_days": "5", "max_age_days": "21"})
    assert ok.headers["location"] == "/opportunities/settings?saved=profile"
    assert "Saved" in client.get(ok.headers["location"]).text


def test_add_and_toggle_boards(client):
    r = client.post("/opportunities/sources", headers=SAME, follow_redirects=False,
                    data={"link": "https://jobs.ashbyhq.com/initech/0f0f0f0f-0000-4000-8000-000000000000", "company": "Initech"})
    assert r.status_code == 303
    page = client.get("/opportunities/settings").text
    assert "Initech" in page
    source_id = re.search(r'id="source-(\d+)">\s*<div class="l">\s*<span class="co wrap" dir="auto">Initech', page).group(1)
    client.post(f"/opportunities/sources/{source_id}/toggle", headers=SAME, data={"enabled": "0"})
    assert re.search(r'Initech <span class="tag">off</span>', client.get("/opportunities/settings").text)
    bad = client.post("/opportunities/sources", headers=SAME, data={"link": "https://www.linkedin.com/jobs/view/1", "company": "x"})
    assert bad.status_code == 400 and "Greenhouse, Lever, Ashby or Comeet" in bad.text


def test_run_detail_explains_every_posting(client):
    run_now(client)
    diag = client.get("/diagnostics").text
    run_id = re.search(r'href="/diagnostics/runs/(\d+)"', diag).group(1)
    page = client.get(f"/diagnostics/runs/{run_id}").text
    for text in ("Boards checked", "Needs geo review", "Hidden: outside Israel", "Already in your tracker",
                 "Hidden: excluded terms", "new york", "embedded", "$0.00"):
        assert text in page
    assert client.get("/diagnostics/runs/999").status_code == 404


@pytest.mark.parametrize("url, data", [
    ("/search/run", {}),
    ("/postings/1/decision", {"decision": "hidden"}),
    ("/opportunities/settings/profile", {"include_terms": "x"}),
    ("/opportunities/sources", {"link": "https://boards.greenhouse.io/x"}),
    ("/opportunities/sources/1/toggle", {"enabled": "0"}),
])
def test_every_write_endpoint_refuses_cross_site_posts(client, boards, url, data):
    assert client.post(url, headers={"Origin": "http://evil.example"}, data=data).status_code == 403
    assert boards.calls == []


def test_feedback_note_and_evaluation_summary(client):
    run_now(client)
    ids = posting_ids(client.get("/opportunities").text)
    r = client.post(f"/postings/{ids[0]}/decision", headers=SAME, follow_redirects=False,
                    data={"decision": "comment", "note": "Relevant: API testing", "view": "verified"})
    assert f"noted={ids[0]}" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "Note saved" in page and ids[0] in posting_ids(page)          # a note changes nothing
    client.post(f"/postings/{ids[0]}/decision", headers=SAME, data={"decision": "applied_now", "note": "good one"})
    client.post(f"/postings/{ids[1]}/decision", headers=SAME, data={"decision": "skipped", "note": "Hardware / embedded"})
    diag = client.get("/diagnostics").text
    assert "Search feedback" in diag and "50% of judged jobs" in diag
    assert "Hardware / embedded" in diag and "Relevant: API testing" in diag and "Wrong match: what to tighten" in diag


def test_not_a_fit_says_whether_the_search_was_wrong_or_it_is_your_choice(client):
    run_now(client)
    page = client.get("/opportunities").text
    assert "Wrong match</b> — the search should not have brought it" in page and "Not for me</b>" in page
    ids = posting_ids(page)
    client.post(f"/postings/{ids[0]}/decision", headers=SAME, data={"decision": "declined", "note": "Salary"})
    client.post(f"/postings/{ids[1]}/decision", headers=SAME, data={"decision": "skipped", "note": "Hardware / embedded"})
    diag = client.get("/diagnostics").text
    assert "search right 50%" in diag and "Not for me: your reasons" in diag and "Salary" in diag
    assert set(ids) <= set(posting_ids(client.get("/opportunities?show=hidden").text))


def test_view_job_is_one_step_before_the_application_form(client):
    run_now(client)
    page = client.get("/opportunities").text
    pid = re.search(r'<article class="job" id="posting-(\d+)">(?:(?!</article>).)*Payments', page, re.S).group(1)
    assert f'href="/postings/{pid}"' in page and "View job" in page
    detail = client.get(f"/postings/{pid}").text
    assert '<section class="jd">' in detail and "Apply ·" in detail       # full description once, apply next to it
    assert '<section class="jd">' not in page                              # the list shows only a short summary


def test_earlier_at_the_company_opens_the_record_and_comes_back_to_the_job(client):
    from urllib.parse import unquote
    run_now(client)
    html = client.get("/opportunities").text
    link = re.search(r'href="(/records/(\d+)\?back=([^"]+))"', html)
    assert link, "a job at a company you applied to links to that record"
    record_url, rid, back = link.group(1), link.group(2), unquote(link.group(3))
    assert back.startswith("/opportunities?tab=verified#posting-")
    page = client.get(record_url).text
    assert "← Back to the job" in page and f'name="back" value="{back}"' in page.replace("&amp;", "&")
    r = client.post(f"/records/{rid}/status", headers=SAME, follow_redirects=False,
                    data={"state": "REJECTED", "note": "rejection e-mail", "back": back})
    assert r.status_code == 303 and r.headers["location"] == back      # straight back to the same job card
    assert "Rejected by them" in client.get("/opportunities").text


def test_already_in_my_tracker_points_to_the_record_to_update(client):
    run_now(client)
    html = client.get("/opportunities").text
    acme = re.search(r'<article class="job" id="posting-(\d+)">(?:(?!</article>).)*Acme Cloud', html, re.S).group(1)
    r = client.post(f"/postings/{acme}/decision", headers=SAME, data={"decision": "already_applied", "view": "verified"})
    assert "Its status lives on your tracker record" in r.text and "/records/" in r.text


def test_back_links_stay_inside_the_console(client):
    page = client.get("/records/5?back=//evil.example/pipeline").text
    assert 'href="/pipeline"' in page and "evil.example" not in page


def test_save_and_maybe_keep_a_reposted_job_and_show_on_the_card(imported_db, monkeypatch):
    from tests.test_search_pipeline import gh
    monkeypatch.setattr("search.pipeline.POLITE_DELAY_S", 0)
    again = {"jobs": [gh(111, "QA Automation Lead (Req 12345)", "Tel Aviv, Israel", requisition_id="12345",
                         published="2026-10-10T08:00:00Z")]}       # applied in August, published again in October
    app = create_app(imported_db, allowed_hosts=("testserver",), search_get=FakeBoards(greenhouse=again))
    client = TestClient(app)
    client.post("/search/run", headers=SAME)
    app.state.search_thread.join(timeout=10)
    pid = posting_ids(client.get("/opportunities").text)[-1]
    for decision in ("maybe", "saved"):
        r = client.post(f"/postings/{pid}/decision", headers=SAME, data={"decision": decision, "view": "verified"})
        assert pid in posting_ids(r.text) and "Reposted by the employer" in r.text   # still there, in its block
        card = re.search(rf'<article class="job" id="posting-{pid}">.*?</article>', r.text, re.S).group(0)
        assert f'<p class="flash" role="status">✓ {"Maybe" if decision == "maybe" else "Saved"}</p>' in card
        assert f'value="{decision}" title="' in card and 'aria-pressed="true"' in card


def test_levels_are_marked_and_your_level_comes_first(client):
    run_now(client)
    html = client.get("/opportunities").text
    assert "engineer role" in html                                   # marked, not hidden
    assert len(posting_ids(html)) == 2


def test_new_jobs_show_possible_contacts(client):
    run_now(client)
    html = client.get("/opportunities").text
    assert "<dt>Contacts</dt>" in html and "People you know there ↗" in html and "linkedin.com/search/results/people/" in html


def test_duplicate_says_which_job_it_repeats(client):
    run_now(client)
    html = client.get("/opportunities").text
    assert ">Duplicate</button>" in html and "Already in my tracker" not in html
    pid = posting_ids(html)[0]
    client.post(f"/postings/{pid}/decision", headers=SAME,
                data={"decision": "already_applied", "note": "", "same_as": "Acme Cloud — QA Engineer (CC 7)", "view": "verified"})
    hidden = client.get("/opportunities?show=hidden").text
    assert "same as Acme Cloud — QA Engineer (CC 7)" in hidden and "Duplicate" in hidden


def test_overqualified_is_a_mark_that_never_hides_the_job(client):
    run_now(client)
    pid = posting_ids(client.get("/opportunities").text)[0]
    r = client.post(f"/postings/{pid}/decision", headers=SAME, data={"decision": "overqualified", "view": "verified"})
    assert pid in posting_ids(r.text)                                       # still listed
    card = re.search(rf'<article class="job" id="posting-{pid}">.*?</article>', r.text, re.S).group(0)
    assert "overqualified (your mark)" in card and "✓ Marked overqualified" in card
    assert 'value="overqualified_off"' in card                             # pressing again removes the mark
    assert "Overqualified</small><b>1</b>" in client.get("/diagnostics").text
    client.post(f"/postings/{pid}/decision", headers=SAME, data={"decision": "applied_now", "view": "verified"})
    hidden = client.get("/opportunities?show=hidden").text                  # you can still apply: the mark stays
    assert pid in posting_ids(hidden) and "overqualified (your mark)" in hidden
    r = client.post(f"/postings/{pid}/decision", headers=SAME, data={"decision": "overqualified_off", "view": "hidden"})
    assert "overqualified (your mark)" not in client.get("/opportunities?show=hidden").text


def test_wrong_match_for_the_whole_company_turns_its_board_off(client):
    run_now(client)
    html = client.get("/opportunities").text
    assert "Wrong match — whole company" in html
    acme = re.search(r'<article class="job" id="posting-(\d+)">(?:(?!</article>).)*Acme Cloud', html, re.S).group(1)
    r = client.post(f"/postings/{acme}/decision", headers=SAME,
                    data={"decision": "skipped", "note": "Hardware", "whole_company": "1", "view": "verified"})
    assert "board is turned off" in r.text and acme not in posting_ids(r.text)
    assert "Turn on" in client.get("/opportunities/settings").text    # listed as off, ready to turn on again
    page = client.get(f"/postings/{acme}").text                         # the decision and its note are kept
    assert "Hardware — whole company is not for me: board turned off" in page


def test_filters_panel_shows_each_rule_and_turns_one_off(client):
    run_now(client)
    html = client.get("/opportunities").text
    assert 'id="filters"' in html and "all 8 on" in html
    assert "Excluded word in the title" in html and "Older than 14 days" in html and "Boards you turned off" in html
    assert "Embedded QA Engineer" not in html
    r = client.post("/opportunities/filters", headers=SAME, follow_redirects=False, data={"filter": "title", "state": "off"})
    assert r.status_code == 303 and r.headers["location"] == "/opportunities?tab=verified&filters=open#filters"
    html = client.get("/opportunities").text
    assert "1 off" in html and "Embedded QA Engineer" in html and "filter off: excluded title word" in html
    assert "1 let in" in html and "Turn on" in html
    # saving Search settings keeps the board filters as they are
    client.post("/opportunities/settings/profile", headers=SAME,
                data={"include_terms": "qa\nquality", "exclude_terms": "embedded", "low_priority_terms": "",
                      "fresh_days": "7", "max_age_days": "14"})
    assert "filter off: excluded title word" in client.get("/opportunities").text
    assert client.post("/opportunities/filters", headers=SAME, data={"filter": "salary", "state": "off"}).status_code == 400


def test_run_search_from_the_main_page_comes_back_to_it(client):
    r = client.post("/search/run", headers=SAME, data={"back": "/pipeline"}, follow_redirects=False)
    assert r.headers["location"] == "/pipeline?run=started#search"
    client.app_ref.state.search_thread.join(timeout=10)
    odd = client.post("/search/run", headers=SAME, data={"back": "https://elsewhere.example"}, follow_redirects=False)
    assert odd.headers["location"].startswith("/opportunities?tab=verified&run=")   # never another site
    client.app_ref.state.search_thread.join(timeout=10)


def test_filters_panel_stays_open_after_turning_the_last_filter_back_on(client):
    run_now(client)
    client.post("/opportunities/filters", headers=SAME, data={"filter": "older", "state": "off"})
    r = client.post("/opportunities/filters", headers=SAME, data={"filter": "older", "state": "on"})
    assert "all 8 on" in r.text and 'id="filters" open' in r.text   # the list is still there to go on
    assert 'id="filters" open' not in client.get("/opportunities").text   # a fresh visit: folded, one line


def test_regions_are_ticked_in_settings(client):
    page = client.get("/opportunities/settings").text
    assert 'name="regions" value="north" checked' in page
    data = {"include_terms": "qa", "exclude_terms": "embedded", "low_priority_terms": "", "fresh_days": "7",
            "max_age_days": "14", "regions_form": "1"}
    form = "&".join(f"{k}={v}" for k, v in data.items()) + "".join(f"&regions={k}" for k in
                                                                     ("tel_aviv", "sharon", "center", "jerusalem", "judea_samaria", "south"))
    r = client.post("/opportunities/settings/profile", headers={**SAME, "Content-Type": "application/x-www-form-urlencoded"},
                    content=form, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/opportunities/settings").text
    assert 'name="regions" value="north" checked' not in page and 'name="regions" value="south" checked' in page


def test_an_application_marked_in_the_console_can_be_rejected(client, imported_db):
    """A good find you applied to (CC) is in My Pipeline as Applied; a rejection e-mail later moves it to Rejected."""
    from app import db, tracker
    from search import pipeline
    run_now(client)
    pid, other = map(int, posting_ids(client.get("/opportunities").text)[:2])
    client.post(f"/postings/{pid}/decision", headers=SAME, data={"decision": "applied_now", "view": "verified"})
    page = client.get(f"/postings/{pid}").text
    assert 'id="status"' in page and f'action="/records/-{pid}/status"' in page
    r = client.post(f"/records/-{pid}/status", headers=SAME, follow_redirects=False,
                    data={"state": "REJECTED", "note": "Rejection e-mail", "back": f"/postings/{pid}#status"})
    assert r.status_code == 303 and r.headers["location"] == f"/postings/{pid}#status"
    page = client.get(f"/postings/{pid}").text
    assert "Rejection e-mail" in page and 'value="REJECTED" title="They said no after you applied" aria-pressed="true"' in page
    assert f"CC {pid}" in client.get("/pipeline?group=closed").text
    assert f"CC {pid}" not in client.get("/pipeline?group=active").text
    conn = db.connect(imported_db)
    hits = [h for hs in pipeline.history_index(conn).by_company.values() for h in hs if h.sheet == pipeline.CONSOLE and h.row == pid]
    assert hits and hits[0].state == "REJECTED"           # "rejected here before" on the next job at this company
    with pytest.raises(ValueError):                        # a posting you did not apply to has no such status
        tracker.record_console_status(conn, other, "REJECTED")
    conn.close()


def test_a_record_can_be_marked_duplicate(client, imported_db):
    from app import db, tracker
    conn = db.connect(imported_db)
    rid = conn.execute("SELECT id FROM legacy_rows WHERE sheet_name='Master Tracker' AND source_row=2").fetchone()[0]
    conn.close()
    assert 'value="DUPLICATE"' in client.get(f"/records/{rid}").text
    client.post(f"/records/{rid}/status", headers=SAME, data={"state": "DUPLICATE", "note": "same job as MT 3"})
    conn = db.connect(imported_db)
    assert tracker.current_statuses(conn)[rid].state == "DUPLICATE"
    conn.close()
