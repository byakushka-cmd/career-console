"""Workday boards (<company>.wd5.myworkdayjobs.com): read through the site's own search, filtered to Israel."""
import json

import pytest

from search import sources

FACETS = [{"facetParameter": "locationMainGroup", "values": [
    {"facetParameter": "locationHierarchy1", "descriptor": "Locations", "values": [
        {"descriptor": "Armenia", "id": "aaa", "count": 2}, {"descriptor": "Israel", "id": "il-123", "count": 3}]}]}]


def job(title, path, where, posted, req):
    return {"title": title, "externalPath": path, "locationsText": where, "postedOn": posted, "bulletFields": [req]}


class FakeWorkday:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload=None):
        assert url == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs" and payload is not None
        self.calls.append(payload)
        if not payload["appliedFacets"]:
            return json.dumps({"total": 900, "jobPostings": [], "facets": FACETS}).encode()
        assert payload["appliedFacets"] == {"locationHierarchy1": ["il-123"]}
        return json.dumps({"total": 3, "jobPostings": [
            job("QA Team Lead", "/job/Israel-Tel-Aviv/QA-Team-Lead_JR2000001", "Israel, Tel Aviv", "Posted Today", "JR2000001"),
            job("Senior SDET", "/job/Israel-Yokneam/Senior-SDET_JR2000002", "2 Locations", "Posted 3 Days Ago", "JR2000002"),
            job("Old Job", "/job/Israel-Haifa/Old-Job_JR1000003", "Israel, Haifa", "Posted 30+ Days Ago", "JR1000003"),
        ]}).encode()


def test_a_workday_board_is_read_in_israel_only():
    get = FakeWorkday()
    jobs = sources.fetch_workday("acme.wd5/AcmeCareers", get)
    assert [(j.title, j.location, j.countries, j.requisition_id) for j in jobs] == [
        ("QA Team Lead", "Israel, Tel Aviv", ["IL"], "JR2000001"),
        ("Senior SDET", "Israel (2 locations)", ["IL"], "JR2000002"),
        ("Old Job", "Israel, Haifa", ["IL"], "JR1000003")]
    assert jobs[0].url == "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/Israel-Tel-Aviv/QA-Team-Lead_JR2000001"
    assert jobs[0].provider_job_id == "acme:JR2000001" and jobs[0].posted_at.endswith("T00:00:00Z")
    assert len(get.calls) == 2      # one page was the last: no more requests


def test_a_site_without_jobs_in_israel_gives_nothing():
    def get(url, payload=None):
        return json.dumps({"total": 5, "jobPostings": [], "facets": [{"facetParameter": "locationHierarchy1",
                           "values": [{"descriptor": "France", "id": "fr"}]}]}).encode()
    assert sources.fetch_workday("acme.wd5/AcmeCareers", get) == []


def test_workday_links_are_boards_and_job_ids():
    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/QA-Manager_JR1234567"
    assert sources.detect_board(url) == ("workday", "nvidia.wd5/NVIDIAExternalCareerSite")
    assert sources.job_identities(url) == {"workday:nvidia:jr1234567"}
    assert sources.posting_identity("workday", "nvidia.wd5/NVIDIAExternalCareerSite", "nvidia:JR1234567") == "workday:nvidia:jr1234567"


def test_only_official_hosts_are_contacted():
    with pytest.raises(sources.SourceError, match="not an allowed"):
        sources.http_get("https://www.linkedin.com/jobs/view/1/")
    with pytest.raises(sources.SourceError, match="not an allowed"):
        sources.http_get("https://evil.example.com/wday/cxs/x/y/jobs", {"limit": 20})


def test_a_workday_jobs_description_is_read_once_for_titles_you_search_for(imported_db):
    from app import db
    from search import pipeline
    conn = db.connect(imported_db)
    _, prof = pipeline.current_profile(conn)
    asked = []

    def get(url, payload=None):
        asked.append(url)
        assert url.startswith("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/job/")
        return json.dumps({"jobPostingInfo": {"jobDescription": "<p>Lead the <b>QA</b> team.</p>", "startDate": "2026-10-12"}}).encode()

    src = {"id": 999, "provider": "workday", "board": "acme.wd5/AcmeCareers"}
    jobs = [sources.Posting("workday", "acme:JR1", "QA Team Lead",
                            "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/Israel/QA-Team-Lead_JR1", posted_at="2026-10-13T00:00:00Z"),
            sources.Posting("workday", "acme:JR2", "Backend Developer",
                            "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/Israel/Backend-Developer_JR2")]
    assert pipeline.workday_details(conn, [(src, jobs, None, "ok", 0)], prof, get, lambda s: None) == 1
    assert jobs[0].description.strip() == "Lead the QA team." and jobs[0].posted_at == "2026-10-12T00:00:00Z"
    assert jobs[1].description == "" and len(asked) == 1        # not a title you search for: not read
    conn.close()


def test_a_lever_job_keeps_its_lists_of_responsibilities_and_requirements():
    job = [{"id": "x", "text": "Validation Engineer", "categories": {"location": "Kinneret"}, "hostedUrl": "https://jobs.lever.co/acme/x",
            "descriptionPlain": "Acme is reimagining networks.",
            "lists": [{"text": "Requirements", "content": "<li>5+ years RF validation</li><li>Python</li>"}],
            "additionalPlain": "We are an equal opportunity employer."}]
    [posting] = sources.fetch_lever("acme", lambda url: json.dumps(job).encode())
    assert posting.description.startswith("Acme is reimagining networks.")
    assert "Requirements" in posting.description and "5+ years RF validation" in posting.description
    assert posting.description.rstrip().endswith("equal opportunity employer.")
