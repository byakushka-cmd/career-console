"""End-to-end search runs against fake official boards (no network)."""
import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from search import pipeline, sources

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
LEVER_ID = "9b1c2d3e-0000-4000-8000-000000000001"


def gh(id_, title, location, published="2026-10-10T08:00:00Z", **extra):
    return {"id": id_, "title": title, "location": {"name": location}, "first_published": published,
            "updated_at": published, "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{id_}",
            "content": "&lt;p&gt;Web and API testing&lt;/p&gt;", **extra}


GREENHOUSE = {"jobs": [
    gh(111, "QA Automation Lead (Req 12345)", "Tel Aviv, Israel", requisition_id="12345",   # applied (tracker row 2)
       published="2026-08-20T08:00:00Z"),
    gh(222, "Senior QA Engineer, Payments", "Tel Aviv-Yafo, Israel"),                      # new, fresh
    gh(333, "QA Lead", "New York, NY"),                                                     # outside Israel
    gh(444, "SDET", "Remote"),                                                              # unknown geography
    gh(555, "Embedded QA Engineer", "Haifa, Israel"),                                       # excluded by rules
    gh(666, "Backend Engineer", "Tel Aviv"),                                                # not a QA role
    gh(777, "Quality Engineer", "Herzliya, Israel", published="2026-07-01T00:00:00Z"),      # older than 2 weeks: kept
    gh(888, "Senior Quality Specialist", "Tel Aviv, Israel",                                # regulatory, per description
       content="&lt;p&gt;Strong knowledge of FDA requirements, MDR regulation, ISO 13485 and IEC 62304.&lt;/p&gt;"),
]}
LEVER = [{"id": LEVER_ID, "text": "Senior QA Engineer", "categories": {"location": "Tel Aviv"},
          "hostedUrl": f"https://jobs.lever.co/globex/{LEVER_ID}", "createdAt": 1791000000000}]
COMEET_PAGE = ("<html><script>\nvar COMPANY_POSITIONS_DATA = " + json.dumps([
    {"uid": "AB.123", "name": "QA Engineer", "location": {"name": "Ramat Gan, Israel", "country": "IL"},
     "time_updated": "2026-10-13T00:00:00Z", "url_comeet_hosted_page": "https://www.comeet.com/jobs/acme/AA.001/qa-engineer/AB.123"}
]) + ";\n</script></html>")


class FakeBoards:
    def __init__(self, fail=None, greenhouse=GREENHOUSE, comeet=COMEET_PAGE):
        self.calls, self.fail, self.greenhouse, self.comeet = [], fail or {}, greenhouse, comeet

    def __call__(self, url):
        self.calls.append(url)
        for key, status in self.fail.items():
            if key in url:
                raise sources.SourceError(f"HTTP {status} (access blocked — not proof that anything closed)", status)
        if "greenhouse" in url:
            return json.dumps(self.greenhouse).encode()
        if "lever" in url:
            return json.dumps(LEVER).encode()
        if "comeet" in url:
            return self.comeet.encode()
        raise AssertionError(url)


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


def run(conn, boards=None, **kw):
    return pipeline.run_search(conn, get=boards or FakeBoards(), now=kw.pop("now", NOW), sleep=lambda s: None, **kw)


def titles(views):
    return {v.posting["title"] for v in views}


def test_sources_and_profile_are_seeded_from_the_tracker(conn):
    boards = {(r["provider"], r["board"]) for r in conn.execute("SELECT * FROM search_sources")}
    assert boards == {("greenhouse", "acme"), ("lever", "globex"), ("comeet", "acme/AA.001")}
    _, prof = pipeline.current_profile(conn)
    assert "qa" in prof["include_terms"] and "embedded" in prof["exclude_terms"]


def test_a_run_presents_only_new_verified_relevant_israel_jobs(conn):
    summary = run(conn)
    assert summary["sources_ok"] == 3 and summary["sources_failed"] == 0 and summary["cost_usd"] == 0
    views = pipeline.opportunities(conn, NOW)
    assert titles(views["verified"]) == {"Senior QA Engineer, Payments", "QA Engineer"}
    assert titles(views["older"]) == {"Quality Engineer"}        # published in July: kept, not shown as new
    assert titles(views["geo_review"]) == {"SDET"}
    assert titles(views["excluded"]) == {"QA Lead"}
    assert titles(views["history"]) == {"QA Automation Lead (Req 12345)",  # applied
                                        "Senior QA Engineer"}              # in your tracker already, not applied
    assert views["reposted"] == []
    assert summary["presented"] == 2


def test_applied_requisition_never_reappears_as_new(conn):
    run(conn)
    candidate = conn.execute("""SELECT c.* FROM run_candidates c JOIN postings p ON p.id=c.posting_id
                                WHERE p.provider_job_id='111'""").fetchone()
    assert candidate["dedup"] == "HISTORY" and not candidate["presented"]
    assert "MT 2 (applied)" in json.loads(candidate["reasons_json"])["dedup"]


def test_every_candidate_path_is_logged_with_reasons(conn):
    summary = run(conn)
    rows = conn.execute("SELECT * FROM run_candidates WHERE run_id=?", (summary["run_id"],)).fetchall()
    assert len(rows) == 10  # 8 Greenhouse + 1 Lever + 1 Comeet: nothing dropped before logging
    by_title = {r["posting_id"]: r for r in rows}
    reasons = {conn.execute("SELECT title FROM postings WHERE id=?", (pid,)).fetchone()[0]: json.loads(r["reasons_json"])
               for pid, r in by_title.items()}
    assert "new york" in reasons["QA Lead"]["geo"]
    assert "embedded" in reasons["Embedded QA Engineer"]["relevance"]
    assert "none of the search terms" in reasons["Backend Engineer"]["relevance"]
    assert "manufacturing-quality terms: fda, mdr" in reasons["Senior Quality Specialist"]["relevance"]


def test_a_role_already_in_your_tracker_is_not_new(conn):
    run(conn)
    lever = next(v for v in pipeline.opportunities(conn, NOW)["history"] if v.provider == "Lever")
    assert lever.ev.dedup in pipeline.KNOWN and "MT 4 (discovered)" in lever.ev.dedup_note
    assert not lever.ev.presented and not lever.ev.reposted   # published Oct 3, before your last update (Oct 5)


def test_older_than_two_weeks_is_kept_but_not_shown_and_comeet_has_only_its_update_date(conn):
    run(conn)
    views = pipeline.opportunities(conn, NOW)
    older = {v.posting["title"]: v for v in views["older"]}
    assert older["Quality Engineer"].ev.freshness.label == "Older than 14 days" and not older["Quality Engineer"].ev.recent
    shown = {v.posting["title"]: v for v in views["verified"]}
    assert shown["Senior QA Engineer, Payments"].ev.freshness.label == "Fresh"
    assert shown["QA Engineer"].ev.freshness.label == "Updated"  # Comeet publishes no posting date, only updates


def test_a_known_job_published_again_is_shown_as_reposted(conn):
    again = {"jobs": [gh(111, "QA Automation Lead (Req 12345)", "Tel Aviv, Israel", requisition_id="12345",
                         published="2026-10-10T08:00:00Z")]}
    run(conn, FakeBoards(greenhouse=again))
    views = pipeline.opportunities(conn, NOW)
    assert "QA Automation Lead (Req 12345)" not in titles(views["verified"])   # never "new"…
    reposted = views["reposted"][0]                                           # …but the employer is still hiring
    assert reposted.posting["title"] == "QA Automation Lead (Req 12345)" and reposted.ev.reposted
    assert (reposted.ev.published_on, reposted.ev.last_action) == ("2026-10-10", "2026-09-27")


def test_your_not_a_fit_comes_back_only_when_published_again(conn):
    run(conn)
    payments = next(v for v in pipeline.opportunities(conn, NOW)["verified"] if "Payments" in v.posting["title"])
    pipeline.record_posting_decision(conn, payments.posting["id"], "declined", "Salary")   # not for me
    later = NOW + timedelta(days=6)
    run(conn, now=later)                                   # same date on the board: stays hidden
    assert payments.posting["id"] in [v.posting["id"] for v in pipeline.opportunities(conn, later)["hidden"]]
    again = {"jobs": [gh(222, "Senior QA Engineer, Payments", "Tel Aviv-Yafo, Israel", published="2026-10-19T08:00:00Z")]}
    later += timedelta(hours=1)                            # boards checked minutes ago are not fetched again
    run(conn, FakeBoards(greenhouse=again), now=later)     # published again after your decision
    reposted = pipeline.opportunities(conn, later)["reposted"]
    assert [v.posting["id"] for v in reposted] == [payments.posting["id"]] and reposted[0].ev.decision == "declined"


def test_a_not_applied_tracker_record_expires_after_two_weeks(conn):
    from app import tracker
    from datetime import date
    assert tracker.effective_state("DISCOVERED", "2026-10-01", date(2026, 10, 14)) == "DISCOVERED"  # 13 days
    assert tracker.effective_state("DISCOVERED", "2026-09-29", date(2026, 10, 14)) == tracker.EXPIRED  # 15 days
    assert tracker.effective_state("HOLD", "2026-09-01", date(2026, 10, 14)) == tracker.EXPIRED
    assert tracker.effective_state("APPLIED", "2026-08-01", date(2026, 10, 14)) == "APPLIED"   # applied stays applied
    assert tracker.effective_state("DISCOVERED", None, date(2026, 10, 14)) == "DISCOVERED"     # no date: left alone


def test_blocked_source_never_closes_a_verified_job(conn):
    run(conn)
    blocked = FakeBoards(fail={"greenhouse": 403})
    summary = run(conn, blocked, now=NOW + timedelta(hours=2))
    assert summary["sources_failed"] == 1 and "403" in summary["errors"][0][1]
    views = {v.posting["title"]: v for v in pipeline.opportunities(conn, NOW + timedelta(hours=2))["verified"]}
    assert "Senior QA Engineer, Payments" in views
    assert "does not mean it closed" in views["Senior QA Engineer, Payments"].ev.verification_note
    check = conn.execute("""SELECT c.* FROM source_checks c JOIN search_sources s ON s.id=c.source_id
                            WHERE s.provider='greenhouse' ORDER BY c.id DESC LIMIT 1""").fetchone()
    assert (check["ok"], check["http_status"]) == (0, 403)


def test_a_posting_removed_from_the_board_stops_being_verified_but_is_kept(conn):
    run(conn)
    fewer = FakeBoards(greenhouse={"jobs": [j for j in GREENHOUSE["jobs"] if j["id"] != 222]})
    run(conn, fewer, now=NOW + timedelta(hours=2))
    assert "Senior QA Engineer, Payments" not in titles(pipeline.opportunities(conn, NOW + timedelta(hours=2))["verified"])
    assert conn.execute("SELECT count(*) FROM postings WHERE provider_job_id='222'").fetchone()[0] == 1


def test_dry_run_writes_nothing(conn):
    summary = run(conn, dry_run=True)
    assert summary["dry_run"] and summary["postings_seen"] == 10
    for table in ("search_runs", "postings", "run_candidates", "source_checks", "cost_ledger"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_recently_checked_boards_are_not_fetched_again_unless_forced(conn):
    run(conn)
    boards = FakeBoards()
    summary = run(conn, boards, now=NOW + timedelta(minutes=5))
    assert boards.calls == [] and summary["sources_cached"] == 3
    run(conn, boards, now=NOW + timedelta(minutes=6), force=True)
    assert len(boards.calls) == 3
    run(conn, FakeBoards(), now=NOW + timedelta(minutes=pipeline.CACHE_MINUTES + 7))  # a quick update later fetches again


def test_only_one_run_at_a_time(conn):
    with db.transaction(conn):
        conn.execute("INSERT INTO search_runs(started_at_utc, status, mode, trigger) VALUES (?, 'running', 'live', 'web')",
                     (NOW.isoformat(),))
    with pytest.raises(pipeline.RunInProgress):
        run(conn)
    run(conn, now=NOW + timedelta(minutes=pipeline.STALE_RUN_MINUTES + 1))  # a stale lock is abandoned, not fatal
    assert conn.execute("SELECT status FROM search_runs ORDER BY id LIMIT 1").fetchone()[0] == "abandoned"


def test_your_decisions_hide_restore_and_override(conn):
    run(conn)
    views = pipeline.opportunities(conn, NOW)
    payments = next(v for v in views["verified"] if v.posting["title"] == "Senior QA Engineer, Payments").posting["id"]
    sdet = views["geo_review"][0].posting["id"]
    pipeline.record_posting_decision(conn, payments, "hidden")
    pipeline.record_posting_decision(conn, sdet, "show_anyway", "Confirmed with recruiter: Israel OK")
    views = pipeline.opportunities(conn, NOW)
    assert "Senior QA Engineer, Payments" in titles(views["hidden"]) and "SDET" in titles(views["verified"])
    pipeline.record_posting_decision(conn, payments, "restored")
    assert "Senior QA Engineer, Payments" in titles(pipeline.opportunities(conn, NOW)["verified"])
    with pytest.raises(ValueError):
        pipeline.record_posting_decision(conn, payments, "delete_everything")


def test_saved_jobs_sort_first(conn):
    run(conn)
    last = pipeline.opportunities(conn, NOW)["verified"][-1]
    pipeline.record_posting_decision(conn, last.posting["id"], "saved")
    assert pipeline.opportunities(conn, NOW)["verified"][0].posting["id"] == last.posting["id"]


def test_profile_changes_are_versioned_and_apply_immediately(conn):
    run(conn)
    _, prof = pipeline.current_profile(conn)
    pipeline.save_profile(conn, {**prof, "exclude_terms": prof["exclude_terms"] + ["payments"]})
    assert "Senior QA Engineer, Payments" not in titles(pipeline.opportunities(conn, NOW)["verified"])
    assert conn.execute("SELECT count(*) FROM search_profile_versions").fetchone()[0] == 2
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM search_profile_versions")


def test_adding_sources_by_link(conn):
    sid = pipeline.add_source(conn, "https://jobs.ashbyhq.com/initech/0f0f0f0f-0000-4000-8000-000000000000", "Initech")
    row = conn.execute("SELECT * FROM search_sources WHERE id=?", (sid,)).fetchone()
    assert (row["provider"], row["board"], row["company"]) == ("ashby", "initech", "Initech")
    with pytest.raises(ValueError):
        pipeline.add_source(conn, "https://www.linkedin.com/jobs/view/123", "LinkedIn")  # not an official ATS board


def test_only_official_ats_hosts_can_be_fetched(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket, "create_connection", no_network)
    for url in ("https://www.linkedin.com/jobs/search", "http://boards-api.greenhouse.io/v1/boards/x/jobs",
                "https://evil.example/boards-api.greenhouse.io"):
        with pytest.raises(sources.SourceError, match="not an allowed"):
            sources.http_get(url)


def test_a_repost_of_a_job_you_applied_to_is_not_new(conn):
    run(conn)
    payments = next(v for v in pipeline.opportunities(conn, NOW)["verified"] if v.posting["title"] == "Senior QA Engineer, Payments")
    pipeline.record_posting_decision(conn, payments.posting["id"], "applied_now")
    repost = FakeBoards(greenhouse={"jobs": GREENHOUSE["jobs"] + [gh(999, "Senior QA Engineer, Payments", "Tel Aviv, Israel")]})
    run(conn, repost, now=NOW + timedelta(hours=2))
    views = pipeline.opportunities(conn, NOW + timedelta(hours=2))
    assert "Senior QA Engineer, Payments" not in titles(views["verified"])
    reposted = next(v for v in views["history"] if v.posting["provider_job_id"] == "999")
    assert reposted.ev.dedup == "POSSIBLE_HISTORY" and "CC" in reposted.ev.dedup_note


def test_comeet_apply_link_points_at_the_position_not_a_generic_page():
    page = ("var COMPANY_POSITIONS_DATA = " + json.dumps([
        {"uid": "AB.123", "name": "QA", "location": {}, "url_comeet_hosted_page": "https://www.comeet.com/jobs/x/AA.001/qa/AB.123",
         "url_active_page": "https://example.com/careers/"},
        {"uid": "CD.456", "name": "QA 2", "location": {}, "url_comeet_hosted_page": "https://www.comeet.com/jobs/x/AA.001/qa-2/CD.456",
         "url_active_page": "https://example.com/job/?id=CD.456"}]) + ";\n")
    postings = sources.fetch_comeet("x/AA.001", lambda url: page.encode())
    assert postings[0].apply_url == "https://www.comeet.com/jobs/x/AA.001/qa/AB.123"   # generic careers page skipped
    assert postings[1].apply_url == "https://example.com/job/?id=CD.456"              # employer page for this position


def test_your_tracker_status_is_used_for_new_postings(conn):
    from app import tracker
    run(conn)
    # tracker row 4 (Globex, Senior QA Engineer) is "discovered"; you mark it rejected -> the Lever posting is history
    row4 = conn.execute("SELECT id FROM legacy_rows WHERE sheet_name='Master Tracker' AND source_row=4").fetchone()[0]
    tracker.record_status(conn, row4, "REJECTED", "rejected by email")
    views = pipeline.opportunities(conn, NOW)
    assert "Senior QA Engineer" not in titles(views["verified"])
    lever = next(v for v in views["history"] if v.provider == "Lever")
    assert lever.ev.dedup == "POSSIBLE_HISTORY" and "MT 4 (rejected)" in lever.ev.dedup_note


def test_applying_through_a_posting_carries_over_to_the_tracker_row(conn):
    from app import tracker
    run(conn)
    # Greenhouse 111 is linked to tracker row 2 by its official ID; it is already applied there, so use the
    # requisition route instead: posting 222 gets requisition 12345 = tracker rows 2/3 (Req 12345)
    conn.execute("BEGIN")
    conn.execute("UPDATE postings SET requisition_id='12345' WHERE provider_job_id='222'")
    conn.execute("COMMIT")
    payments = conn.execute("SELECT id FROM postings WHERE provider_job_id='222'").fetchone()[0]
    pipeline.record_posting_decision(conn, payments, "applied_now")
    carried = pipeline.tracker_rows_applied_via_postings(conn)
    rows = {conn.execute("SELECT source_row FROM legacy_rows WHERE id=?", (rid,)).fetchone()[0] for rid in carried}
    assert rows == {2, 3}
    statuses = tracker.current_statuses(conn)
    assert all(statuses[rid].state == "APPLIED" and statuses[rid].source == "posting" for rid in carried)


def test_jobs_that_appear_after_the_first_check_are_marked_new_and_come_first(conn):
    run(conn)
    assert not any(v.is_new for v in pipeline.opportunities(conn, NOW)["verified"])  # first check: all pre-existing
    later = NOW + timedelta(hours=3)
    arrived = FakeBoards(greenhouse={"jobs": GREENHOUSE["jobs"] + [gh(1001, "QA Automation Engineer", "Haifa, Israel",
                                                                          published=later.isoformat())]})
    run(conn, arrived, now=later)
    views = pipeline.opportunities(conn, later)["verified"]
    assert views[0].posting["title"] == "QA Automation Engineer" and views[0].is_new
    assert [v.posting["title"] for v in views if v.is_new] == ["QA Automation Engineer"]
    assert not any(v.is_new for v in pipeline.opportunities(conn, later + timedelta(hours=pipeline.NEW_HOURS + 1))["verified"])


def test_already_in_my_tracker_is_a_duplicate_not_an_application(conn):
    run(conn)
    payments = conn.execute("SELECT id FROM postings WHERE provider_job_id='222'").fetchone()[0]
    pipeline.record_posting_decision(conn, payments, "already_applied", "I had this one since September")
    assert pipeline.applied_postings(conn) == [] and pipeline.tracker_rows_applied_via_postings(conn) == {}
    hidden = pipeline.opportunities(conn, NOW)["hidden"]
    assert [(v.posting["id"], v.ev.decision) for v in hidden] == [(payments, "already_applied")]


def test_a_note_never_changes_the_decision(conn):
    run(conn)
    payments = conn.execute("SELECT id FROM postings WHERE provider_job_id='222'").fetchone()[0]
    pipeline.record_posting_decision(conn, payments, "saved")
    pipeline.record_posting_decision(conn, payments, "comment", "Relevant: payments API, strong fit")
    assert pipeline.latest_decisions(conn)[payments] == "saved"
    with pytest.raises(ValueError):
        pipeline.record_posting_decision(conn, payments, "comment", "   ")


def test_a_rejection_by_title_outweighs_an_id_match_that_was_only_on_hold():
    h = pipeline.History()
    h.by_identity["greenhouse:1"] = [pipeline.TrackerHit(1, "Master Tracker", 173, "HOLD")]
    h.by_title[("pied piper", "ai-first qa automation engineer")] = [pipeline.TrackerHit(2, "Master Tracker", 36, "REJECTED")]
    verdict, _, note = h.classify("greenhouse:1", "Pied Piper", "AI-First QA Automation Engineer", None)
    assert verdict == "POSSIBLE_HISTORY" and "MT 36 (rejected)" in note
    h.by_title.clear()
    assert h.classify("greenhouse:1", "Pied Piper", "AI-First QA Automation Engineer", None)[0] == "REOPENED"


def test_earlier_records_at_a_company_rejection_first(imported_db):
    conn = db.connect(imported_db)
    try:
        h = pipeline.history_index(conn)
        acme = h.at_company("Acme Cloud")
        assert [(x.ref, x.state) for x in acme][:2] == [("MT 3", "REJECTED"), ("MT 2", "APPLIED")]
        assert acme[1].role == "QA Automation Lead (Req 12345)" and acme[1].applied == "2026-08-31"
        assert h.at_company("Acme Cloud / payments team")[:2] == acme[:2]   # the employer part of the name
        assert h.at_company("Nobody Ltd") == []
        h.agency_terms = ["acme"]
        assert h.at_company("Acme Cloud") == []   # agencies: their records are about many clients
    finally:
        conn.close()


LONG_TEXT = ("About the role: we are looking for a hands-on engineer to design, build and run automated tests for our "
             "cloud platform, own regression suites, review pull requests, mentor teammates, improve release quality, "
             "measure flakiness, work with product managers, define acceptance criteria, investigate production "
             "incidents, and keep the pipeline green across browsers, services, mobile clients and public APIs.")


def test_comeet_descriptions_are_read_from_the_page():
    page = ("<script>\nvar COMPANY_POSITIONS_DATA = " + json.dumps([
        {"uid": "AB.123", "name": "QA Engineer", "location": {"name": "Tel Aviv", "country": "IL"},
         "url_comeet_hosted_page": "https://www.comeet.com/jobs/acme/AA.001/qa-engineer/AB.123",
         "custom_fields": {"details": [{"name": "Description", "value": "<p>Test the <b>payments</b> API.</p>"},
                                       {"name": "Requirements", "value": "<ul><li>5 years of QA</li></ul>"}]}}]) + ";\n</script>")
    [posting] = sources.fetch_comeet("acme/AA.001", get=lambda url: page.encode())
    assert "Test the payments API." in posting.description and "Requirements" in posting.description
    assert "5 years of QA" in posting.description


def test_a_copy_of_a_job_you_decided_is_the_same_position(conn):
    first = {"jobs": [gh(901, "Test Engineer - Glilot", "Glilot, Israel", content=LONG_TEXT)]}
    run(conn, FakeBoards(greenhouse=first))
    [glilot] = [v for v in pipeline.opportunities(conn, NOW)["verified"] if v.posting["provider_job_id"] == "901"]
    pipeline.record_posting_decision(conn, glilot.posting["id"], "already_applied")
    both = {"jobs": first["jobs"] + [gh(902, "Test Engineer- Tel Aviv", "Tel Aviv, Israel", content=LONG_TEXT)]}
    run(conn, FakeBoards(greenhouse=both), now=NOW + timedelta(hours=1))
    views = pipeline.opportunities(conn, NOW + timedelta(hours=1))
    copy = next(v for v in views["history"] if v.posting["provider_job_id"] == "902")
    assert copy.ev.dedup == "SAME_TEXT" and "same position as" in copy.ev.dedup_note and not copy.ev.presented


def test_tracker_titles_are_compared_without_their_notes(conn):
    h = pipeline.history_index(conn)
    # tracker MT 2 is "QA Automation Lead (Req 12345)", applied; the board lists it with a city instead
    verdict, hits, _ = h.classify("greenhouse:555555", "Acme Cloud", "QA Automation Lead - Tel Aviv", None)
    assert verdict == "POSSIBLE_HISTORY" and "MT 2" in {h_.ref for h_ in hits}
    assert pipeline.core_title("QA / E2E Engineer — LinkedIn #4474683430") == "qa / e2e engineer"


def test_your_level_is_listed_first_and_nothing_is_hidden_for_its_level(conn):
    jobs = {"jobs": [gh(701, "QA Automation Engineer", "Tel Aviv, Israel"), gh(702, "Director of QA", "Tel Aviv, Israel"),
                     gh(703, "QA Team Lead", "Tel Aviv, Israel")]}
    run(conn, FakeBoards(greenhouse=jobs))
    shown = [(v.posting["title"], v.ev.level) for v in pipeline.opportunities(conn, NOW)["verified"]
             if v.provider == "Greenhouse"]
    assert shown == [("QA Team Lead", "FOCUS"), ("QA Automation Engineer", "ENGINEER"), ("Director of QA", "ABOVE")]


def test_a_board_link_you_saved_joins_the_search(conn):
    from search import leads
    leads.capture(conn, "https://www.linkedin.com/posts/someone_hiring-activity-7251111111111111111-abcd",
                  company="Initech", excerpt="Apply here: https://job-boards.greenhouse.io/initech/jobs/12345",
                  job_location="Tel Aviv")
    run(conn, FakeBoards(fail={"initech": 404}))        # the run collects it (the fake board itself is unreachable)
    boards = {(r["provider"], r["board"]): r for r in conn.execute("SELECT * FROM search_sources")}
    assert boards[("greenhouse", "initech")]["origin"] == "a link you saved"
    assert boards[("greenhouse", "initech")]["company"] == "Initech"
    conn.execute("UPDATE search_sources SET enabled=0 WHERE board='initech'")
    assert pipeline.collect_board_links(conn) == 0                      # turned off stays off


ROLE_TEXT = ("About us\nAcme builds payment software used by millions of people every single day around the world.\n"
             "Responsibilities\n" + LONG_TEXT + "\nBenefits\nHybrid work, a gym, lunches and a great team in Tel Aviv.")


def test_a_lead_version_of_a_job_you_skipped_is_a_new_job(conn):
    first = {"jobs": [gh(911, "QA Engineer", "Tel Aviv, Israel", content=ROLE_TEXT)]}
    run(conn, FakeBoards(greenhouse=first))
    [engineer] = [v for v in pipeline.opportunities(conn, NOW)["verified"] if v.posting["provider_job_id"] == "911"]
    pipeline.record_posting_decision(conn, engineer.posting["id"], "declined", "Engineer, not a lead role")
    both = {"jobs": first["jobs"] + [gh(912, "QA Team Lead", "Tel Aviv, Israel", content=ROLE_TEXT)]}
    run(conn, FakeBoards(greenhouse=both), now=NOW + timedelta(hours=1))
    lead = next(v for v in pipeline.opportunities(conn, NOW + timedelta(hours=1))["verified"]
                if v.posting["provider_job_id"] == "912")
    assert lead.ev.dedup == "NEW" and lead.ev.level == "FOCUS"          # same text, another level: never merged


def test_a_similar_ad_for_another_role_is_not_merged(conn):
    first = {"jobs": [gh(921, "QA Automation Engineer", "Tel Aviv, Israel", content=ROLE_TEXT)]}
    run(conn, FakeBoards(greenhouse=first))
    [auto] = [v for v in pipeline.opportunities(conn, NOW)["verified"] if v.posting["provider_job_id"] == "921"]
    pipeline.record_posting_decision(conn, auto.posting["id"], "already_applied")
    reworded = ROLE_TEXT.replace("run automated tests", "run performance and load tests")
    both = {"jobs": first["jobs"] + [gh(922, "QA Performance Engineer", "Tel Aviv, Israel", content=reworded)]}
    run(conn, FakeBoards(greenhouse=both), now=NOW + timedelta(hours=1))
    perf = next(v for v in pipeline.opportunities(conn, NOW + timedelta(hours=1))["verified"]
                if v.posting["provider_job_id"] == "922")
    assert perf.ev.dedup == "NEW"     # very alike text, but another title: shown, at most "possibly the same"


def test_role_text_leaves_the_employer_template_out():
    from search import similar
    role = similar.role_part("x", ROLE_TEXT)
    assert "regression suites" in role and "payment software" not in role and "gym" not in role


def test_board_filters_hold_relevant_jobs_back_and_each_can_be_turned_off(conn):
    run(conn)
    views = pipeline.opportunities(conn, NOW)
    held = views["filter_counts"]   # live jobs each rule alone holds back
    assert held["title"] == 1 and held["description"] == 1 and held["older"] == 1
    assert held["outside"] == 1 and held["geo_unclear"] == 1 and held["known"] >= 1
    assert "Embedded QA Engineer" not in titles(views["verified"])
    pipeline.set_filter(conn, "title", on=False)
    views = pipeline.opportunities(conn, NOW)
    embedded = next(v for v in views["verified"] if v.posting["title"] == "Embedded QA Engineer")
    assert embedded.ev.let_through == ["title"] and views["filter_counts"]["title"] == 1   # now: let in
    assert views["verified"][-1] is embedded                       # after the jobs every filter lets through
    assert pipeline.current_profile(conn)[1]["filters_off"] == ["title"]
    pipeline.set_filter(conn, "title", on=True)
    assert "Embedded QA Engineer" not in titles(pipeline.opportunities(conn, NOW)["verified"])


def test_a_job_held_back_by_two_filters_needs_both_off(conn):
    run(conn)
    applied = "QA Automation Lead (Req 12345)"   # in your tracker, and published in August
    pipeline.set_filter(conn, "known", on=False)
    assert applied not in titles(pipeline.opportunities(conn, NOW)["verified"])
    pipeline.set_filter(conn, "older", on=False)
    [view] = [v for v in pipeline.opportunities(conn, NOW)["verified"] if v.posting["title"] == applied]
    assert set(view.ev.let_through) == {"known", "older"}
    assert "Quality Engineer" in titles(pipeline.opportunities(conn, NOW)["verified"])   # older alone


def test_unknown_filter_names_are_refused_and_dropped(conn):
    with pytest.raises(ValueError):
        pipeline.set_filter(conn, "salary", on=False)
    from search.profile import validate_profile, DEFAULT_PROFILE
    assert validate_profile({**DEFAULT_PROFILE, "filters_off": ["older", "salary"]})["filters_off"] == ["older"]


def test_a_place_you_excluded_holds_a_job_back(conn):
    run(conn)
    _, prof = pipeline.current_profile(conn)
    pipeline.save_profile(conn, {**prof, "exclude_places": ["Tel Aviv"]})
    views = pipeline.opportunities(conn, NOW)
    assert "Senior QA Engineer, Payments" not in titles(views["verified"])        # Tel Aviv-Yafo, Israel
    assert views["filter_counts"]["place"] >= 1
    pipeline.set_filter(conn, "place", on=False)
    payments = next(v for v in pipeline.opportunities(conn, NOW)["verified"] if v.posting["title"] == "Senior QA Engineer, Payments")
    assert payments.ev.let_through == ["place"]


def test_a_comeet_edit_after_your_decision_does_not_bring_the_job_back(conn):
    """Comeet gives only the time of its last edit, which changes almost daily: that is not a new publication
    (a decided job came back as "reposted" every day and was marked Duplicate again and again)."""
    run(conn)
    qa = next(v for v in pipeline.opportunities(conn, NOW)["verified"] if v.provider == "Comeet")
    conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                    VALUES (?, 'posting', ?, 'already_applied', '', 'web')""", ((NOW + timedelta(hours=1)).isoformat(), qa.posting["id"]))
    later = NOW + timedelta(days=3)
    run(conn, FakeBoards(comeet=COMEET_PAGE.replace("2026-10-13T00:00:00Z", "2026-10-16T00:00:00Z")), now=later)
    views = pipeline.opportunities(conn, later)
    assert qa.posting["id"] not in [v.posting["id"] for v in views["reposted"] + views["verified"]]
    assert qa.posting["id"] in [v.posting["id"] for v in views["hidden"]]


JUNIOR = "&lt;p&gt;Requirements: 2+ years of experience in QA automation. Python.&lt;/p&gt;"


def test_a_job_asking_one_or_two_years_of_qa_is_held_back_as_junior(conn):
    from search import profile
    assert profile.asks_junior_years("Requirements: 1-2 years of experience in software testing")
    assert not profile.asks_junior_years("2+ years of QA experience\n2+ years of experience managing a QA team")
    assert not profile.asks_junior_years("5+ years of experience in QA")
    jobs = {"jobs": [gh(901, "QA Automation Engineer", "Tel Aviv, Israel", content=JUNIOR),
                     gh(902, "Senior QA Engineer", "Tel Aviv, Israel", content="&lt;p&gt;6+ years of QA experience&lt;/p&gt;")]}
    run(conn, FakeBoards(greenhouse=jobs))
    views = pipeline.opportunities(conn, NOW)
    assert "QA Automation Engineer" not in titles(views["verified"]) and "Senior QA Engineer" in titles(views["verified"])
    assert views["filter_counts"]["junior"] == 1
    pipeline.set_filter(conn, "junior", on=False)
    junior = next(v for v in pipeline.opportunities(conn, NOW)["verified"] if v.posting["title"] == "QA Automation Engineer")
    assert junior.ev.let_through == ["junior"]


def test_a_leading_organization_is_not_a_lead_role():
    from search.profile import DEFAULT_PROFILE, level
    assert level("בודק/ת תוכנה ( QA ) לארגון ממשלתי מוביל בירושלים", DEFAULT_PROFILE) == "ENGINEER"
    assert level("מוביל/ה טכנולוגי/ת QA & Automation משולב AI", DEFAULT_PROFILE) == "FOCUS"
    assert level("דרוש/ה מוביל/ה תחום QA", DEFAULT_PROFILE) == "FOCUS"
    assert level("ראש צוות QA לחברה מובילה", DEFAULT_PROFILE) == "FOCUS"
