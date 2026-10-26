import re

import pytest
from fastapi.testclient import TestClient

from app.web import create_app

SAME = {"Origin": "http://testserver"}
POST = "https://www.linkedin.com/feed/update/urn:li:activity:7251234567890123456/"


@pytest.fixture
def client(imported_db):
    return TestClient(create_app(imported_db, allowed_hosts=("testserver",)))


def add(client, **fields):
    return client.post("/leads", headers=SAME, follow_redirects=False, data={"kind": "POST_LEAD", **fields})


def test_capture_shows_israel_leads_and_keeps_others_out(client):
    r = add(client, post_url=POST, author="Dana Levi", author_role="Recruiter", role_signal="QA Team Lead",
            job_location="Herzliya", excerpt="We're hiring!")
    assert r.status_code == 303 and "lead_added=" in r.headers["location"]
    add(client, post_url="https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000009/",
        role_signal="US QA Manager", author_role="Recruiter in Tel Aviv", job_location="Austin, TX")
    add(client, post_url="https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000010/",
        role_signal="Mystery QA role", job_location="")
    page = client.get("/opportunities?tab=leads").text
    shown = page.split('id="leads"')[1].split("Needs geo review")[0]
    assert "QA Team Lead" in shown and "US QA Manager" not in shown and "Mystery QA role" not in shown
    assert "Needs geo review" in page and "Mystery QA role" in page
    assert re.search(r"Potential Leads <b>1</b>", page)
    diag = client.get("/diagnostics").text
    assert "US QA Manager" in diag and "austin" in diag          # visible with its geo reason
    assert "Potential leads (1)" in client.get("/pipeline").text
    assert "QA Team Lead" not in client.get("/opportunities?tab=verified").text  # never a verified job


def test_same_post_again_is_merged(client):
    add(client, post_url=POST, role_signal="QA Team Lead", job_location="Herzliya")
    r = add(client, post_url=POST + "?utm_source=share", excerpt="still hiring")
    assert "merged=1" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "Already captured" in page and "seen ×2" in page


def test_lead_form_errors_keep_what_you_typed(client):
    r = add(client, post_url="not a link", role_signal="Typed role")
    assert r.status_code == 400 and "Not added" in r.text and 'value="Typed role"' in r.text


def test_lead_decisions_and_cross_site_refusal(client):
    add(client, post_url=POST, role_signal="QA Team Lead", job_location="Haifa")
    lead_id = re.search(r'id="lead-(\d+)"', client.get("/opportunities?tab=leads").text).group(1)
    client.post(f"/leads/{lead_id}/decision", headers=SAME, data={"decision": "contacted"})
    assert "Mark contacted" in client.get("/opportunities?tab=leads").text
    assert "Contacted" in client.get("/opportunities?tab=leads").text
    assert client.post(f"/leads/{lead_id}/decision", headers={"Origin": "http://evil.example"},
                       data={"decision": "hidden"}).status_code == 403
    assert client.post("/leads", headers={"Origin": "http://evil.example"}, data={"post_url": POST}).status_code == 403
