"""Jobs from job-alert e-mails: parsed from synthetic e-mails in LinkedIn's and AllJobs' formats, read from a fake
mailbox that records how it was opened (read-only, nothing marked)."""
import imaplib
import re
import stat
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app import added, db
from app.web import create_app
from search import mail, pipeline, sources

SAME = {"Origin": "http://testserver"}

DIGEST = """Your job alert for Quality Assurance Team Lead in Tel Aviv-Yafo
New jobs match your preferences.

QA Team Lead
Acme Cloud
Tel Aviv-Yafo
Top applicant
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/4470000001/?trackingId=abc&refId=x

---------------------------------------------------------

Hardware Validation Team Leader
Globex Silicon
Haifa

This company is actively hiring
View job: https://www.linkedin.com/comm/jobs/view/4470000002/?trackingId=def

---------------------------------------------------------

QA Automation Lead (Req 12345)
Acme Cloud
Tel Aviv District, Israel
2 connections
View job: https://www.linkedin.com/comm/jobs/view/4470000003/?trk=x

---------------------------------------------------------

Senior QA Engineer
Initech
Berlin, Germany
View job: https://www.linkedin.com/comm/jobs/view/4470000004/?trk=y

---------------------------------------------------------

See all jobs on LinkedIn: https://www.linkedin.com/comm/jobs/search-results/?keywords=QA

----------------------------------------

This email was intended for Test Person (QA lead)
You are receiving Job Alert emails.
"""

ALLJOBS = """| |
| Test שלום, |
| להלן משרה שפורסמה באתר ומתאימה להגדרות החיפוש שלך |

| |
| ראש צוות QA |
| חברה: אחר |
| מקום העבודה: מספר אזורים |
| סוג משרה: משרה מלאה |

| |
| דרוש/ה ראש צוות QA תיאור התפקיד: ... |
| לצפייה במשרה [](https://www.alljobs.co.il/User/MailsRedirect/DailyMailRedirect_with_DL.aspx?UserID=1&JobID=8858223_1123975473_AlertPackage&JobIDs=8858223&Key=x) |
"""


def message(sender, subject, body, mid, date="Wed, 14 Oct 2026 09:33:31 +0000"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"], m["Message-ID"] = sender, subject, date, mid
    m.set_content(body)
    return bytes(m)


class FakeImap:
    """Gmail as the console sees it. Fails the test if the mailbox is opened for writing or a message is marked."""
    def __init__(self, messages, password="abcdabcdabcdabcd"):
        self.messages, self.password, self.calls = messages, password, []

    def __call__(self, host):
        assert host == "imap.gmail.com"
        return self

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, box, readonly=False):
        assert readonly, "the mailbox must be opened read-only"
        self.calls.append(("select", box))
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria):
        self.calls.append(("search", criteria))
        return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]

    def fetch(self, num, spec):
        assert "PEEK" in spec, "a fetch without PEEK marks the e-mail as read"
        return "OK", [(b"1 (BODY[] {1}", self.messages[int(num) - 1]), b")"]

    def logout(self):
        pass


def test_a_linkedin_digest_gives_title_company_place_and_a_clean_link():
    jobs = mail.parse_linkedin(DIGEST)
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("QA Team Lead", "Acme Cloud", "Tel Aviv-Yafo"),
        ("Hardware Validation Team Leader", "Globex Silicon", "Haifa"),
        ("QA Automation Lead (Req 12345)", "Acme Cloud", "Tel Aviv District, Israel"),
        ("Senior QA Engineer", "Initech", "Berlin, Germany")]
    assert jobs[0].identity == "linkedin:4470000001" and jobs[0].url == "https://www.linkedin.com/jobs/view/4470000001/"


SUGGESTIONS = """Expand your search

Recommendations based on your activity.

Software jobs
https://www.linkedin.com/comm/jobs/search-results/?keywords=Quality+Assurance+Manager

QA Team Lead
Initrode Group
Tel Aviv-Yafo
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/4470000055/?trackingId=x

---------------------------------------------------------

View all jobs: https://www.linkedin.com/comm/jobs/search-results/?keywords=Quality+Assurance+Manager

Gaming jobs
https://www.linkedin.com/comm/jobs/search-results/?keywords=Quality+Assurance+Manager

Project Quality Manager - EPC Tel Aviv Green Line
Alstom
Holon
View job: https://www.linkedin.com/comm/jobs/view/4470825892/?trackingId=y
"""


def test_section_headers_before_a_job_are_not_its_title():
    jobs = mail.parse_linkedin(SUGGESTIONS)
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("QA Team Lead", "Initrode Group", "Tel Aviv-Yafo"),
        ("Project Quality Manager - EPC Tel Aviv Green Line", "Alstom", "Holon")]


def test_a_one_job_linkedin_email_is_read_from_its_subject():
    body = "Apply now https://www.linkedin.com/comm/jobs/view/4470000009/?x\nView jobs in Israel"
    [job] = mail.parse_linkedin(body, "You may be a fit for Initech’s QA Manager role - Actively recruiting")
    assert (job.company, job.title, job.location, job.identity) == ("Initech", "QA Manager", "Israel", "linkedin:4470000009")


def test_an_alljobs_alert():
    [job] = mail.parse_alljobs(ALLJOBS)
    assert (job.title, job.company, job.location) == ("ראש צוות QA", "", "מספר אזורים")
    assert job.identity == "alljobs:8858223" and job.url.endswith("JobID=8858223")


DIGEST_HTML = """<html><body><table><tr><td><b>עבור תחום חיפוש: </b>אבטחת איכות QA</td></tr>
<tr><td><a href="http://www.alljobs.co.il/User/MailsRedirect/DailyMailRedirect_with_DL.aspx?UserID=1&amp;JobID=8849907_0_Daily&amp;Key=x" title="למשרה">
<table><tr><td><a href="http://www.alljobs.co.il/User/MailsRedirect/DailyMailRedirect_with_DL.aspx?UserID=1&amp;JobID=8849907_0_Daily&amp;Key=x">ONE מגייסת לארגון בירושלים בודק /ת  תוכנה  SAP</a></td></tr>
<tr><td><strong>חברה: </strong>מש"א / הדרכה ...
   | <strong>אזור: </strong>ירושלים וסביבתה</td></tr></table>
<a href="http://www.alljobs.co.il/User/MailsRedirect/DailyMailRedirect_with_DL.aspx?UserID=1&amp;JobID=8849907_0_Daily&amp;Key=x"><font>></font></a></a></td></tr>
<tr><td><a href="http://www.alljobs.co.il/x.aspx?JobID=8854731_0_Daily">
<table><tr><td><a href="http://www.alljobs.co.il/x.aspx?JobID=8854731_0_Daily">ראש צוות בדיקות</a></td></tr>
<tr><td><strong>חברה: </strong>Tikal | <strong>אזור: </strong>תל אביב</td></tr></table></a></td></tr></table></body></html>"""


def test_an_alljobs_daily_digest_is_read_from_its_html():
    jobs = mail.parse_alljobs_html(DIGEST_HTML)
    assert [(j.identity, j.title, j.company, j.location) for j in jobs] == [
        ("alljobs:8849907", "ONE מגייסת לארגון בירושלים בודק /ת תוכנה SAP", "", "ירושלים וסביבתה"),
        ("alljobs:8854731", "ראש צוות בדיקות", "Tikal", "תל אביב")]


SINGLE_HTML = """<html><body><table><tr><td>Test שלום,</td></tr><tr><td>להלן משרה שפורסמה באתר ומתאימה להגדרות החיפוש שלך</td></tr>
<tr><td><table><tr><td style="font-weight:bold">מהנדס/ת בדיקות  תוכנה</td></tr>
<tr><td><span><strong>חברה: </strong></span>אחר</td></tr>
<tr><td><strong>מקום העבודה: </strong>
      מספר אזורים
    </td></tr><tr><td><strong>סוג משרה: </strong>משרה מלאה</td></tr></table></td></tr>
<tr><td>דרוש/ה מהנדס/ת בדיקות  תוכנה <br />תיאור התפקיד:<br />* המשרה כוללת...</td></tr>
<tr><td><a href="https://www.alljobs.co.il/User/MailsRedirect/DailyMailRedirect_with_DL.aspx?UserID=1&JobID=8858223_1123975473_AlertPackage&JobIDs=8858223&Key=x" title="לצפייה במשרה">
   לצפייה במשרה </a></td></tr></table></body></html>"""


def html_message(body, mid):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = "Alljobs@alljob.co.il", "עלתה משרה חדשה", mid
    m.set_content(body, subtype="html")
    return m


@pytest.mark.parametrize("body, expected", [
    (SINGLE_HTML, [("alljobs:8858223", "מהנדס/ת בדיקות תוכנה", "", "מספר אזורים")]),
    (DIGEST_HTML, [("alljobs:8849907", "ONE מגייסת לארגון בירושלים בודק /ת תוכנה SAP", "", "ירושלים וסביבתה"),
                   ("alljobs:8854731", "ראש צוות בדיקות", "Tikal", "תל אביב")]),
])
def test_alljobs_sends_html_only_and_both_kinds_are_read(body, expected):
    jobs = mail.jobs_in(html_message(body, "<x@aj>"))
    assert [(j.identity, j.title, j.company, j.location) for j in jobs] == expected



# Drushim's smart agent, in its HTML layout (synthetic values): a link around the title, then company, places, scope.
DRUSHIM_HTML = """<html><body><table dir="rtl"><tr><td>היי Test,</td></tr>
<tr><td><a href="https://www.drushim.co.il/personal/myboard.aspx?agentscodes=1&amp;utm_source=alerts">ראש צוות QA, בדיקות אוטומטיות בתל אביב 30 ק"מ</a></td></tr>
<tr><td><div dir="rtl"><a name="job_38000001" href="https://www.drushim.co.il/job/38000001/ab12cd34/?sag=1&amp;utm_source=alerts&amp;sct=x">ראש/ת צוות QA &amp; Automation</a>
<table dir="rtl"><tr align="right"><td><span>Acme Systems</span><span><div dir="rtl"><span dir="rtl">תל אביב, פתח תקווה</span></div></span>
<span>משרה מלאה</span><span>5-6 שנים</span></td></tr></table></div>
<div dir="rtl">הובלת צוות בדיקות...</div>
<a href="https://www.drushim.co.il/%d7%93-acme/?utm_source=alerts">משרות נוספות בAcme Systems »</a>
<a href="https://www.drushim.co.il/job/38000001/ab12cd34/?sag=1&amp;utm_source=alerts">לצפייה במשרה</a></td></tr>
<tr><td><div dir="rtl"><a name="job_38000002" href="https://www.drushim.co.il/job/38000002/ef56ab78/?utm_source=alerts">בודק/ת תוכנה ידני</a>
<table dir="rtl"><tr align="right"><td><span>חסוי</span><span><div dir="rtl"><span dir="rtl">באר יעקב</span></div></span>
<span>משרה מלאה</span><span>1-2 שנים</span></td></tr></table></div>
<a href="https://www.drushim.co.il/job/38000002/ef56ab78/?utm_source=alerts">לצפייה במשרה</a></td></tr>
<tr><td><a href="https://www.drushim.co.il/my-jobs/unsubscription/?ref=35">להסרה מדיוורים</a></td></tr></table></body></html>"""


def drushim_message(body, mid="<d1@drushim>"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = "הסוכנת מדרושים IL <alerts@drushim.co.il>", "2 משרות חדשות שפורסמו עכשיו בדרושים IL", mid
    m["Date"] = "Wed, 14 Oct 2026 09:33:31 +0000"
    m.set_content(body, subtype="html")
    return m


def test_a_drushim_alert_is_read_from_its_html():
    jobs = mail.jobs_in(drushim_message(DRUSHIM_HTML))
    assert [(j.identity, j.source, j.title, j.company, j.location, j.url) for j in jobs] == [
        ("drushim:38000001", "Drushim", "ראש/ת צוות QA & Automation", "Acme Systems", "תל אביב, פתח תקווה",
         "https://www.drushim.co.il/job/38000001/ab12cd34/"),      # the job's own link, without the tracking part
        ("drushim:38000002", "Drushim", "בודק/ת תוכנה ידני", "", "באר יעקב",
         "https://www.drushim.co.il/job/38000002/ef56ab78/")]     # a confidential company ("חסוי") is not named
    # Drushim's other e-mails (sign-up reminders, account mail) have no jobs
    assert mail.jobs_in(drushim_message("<p>הרשמו לסוכן החכם</p><a href='https://www.drushim.co.il/smartalerts.aspx'>הרשם</a>")) == []


def test_a_drushim_job_in_your_tracker_is_known_by_its_number():
    assert sources.job_identities("Agency #8073, same as Drushim #38000001") == {"drushim:38000001"}
    assert sources.job_identities("https://www.drushim.co.il/job/38000002/ef56ab78/") == {"drushim:38000002"}


def test_drushim_jobs_are_in_israel_even_when_the_place_is_not_known(conn):
    mail.store(conn, "test.person@gmail.com", [drushim_message(DRUSHIM_HTML).as_bytes()])
    v = mail.views(conn)
    assert {x.job["identity"] for x in v["shown"]} == {"drushim:38000001", "drushim:38000002"}

@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


@pytest.fixture
def mailbox(imported_db):
    mail.save_mailbox(mail.config_path(imported_db), "test.person@gmail.com", "abcd abcd abcd abcd")
    return FakeImap([message("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "QA Team Lead at Acme Cloud", DIGEST, "<a1@li>"),
                     message("Alljobs@alljob.co.il", "Test, עלתה משרה חדשה", ALLJOBS, "<b1@aj>"),
                     message("Someone <friend@example.com>", "Hello", "not an alert", "<c1@x>")])


def test_reading_the_mailbox_is_read_only_and_counts_each_email_once(conn, imported_db, mailbox):
    path = mail.config_path(imported_db)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    [r] = mail.read(conn, imported_db, mailbox)
    assert r["ok"] and r["new_jobs"] == 5 and r["messages"] == 3
    assert ("select", '"[Gmail]/All Mail"') in mailbox.calls
    assert any("newer_than:2d" in " ".join(c[1]) for c in mailbox.calls if c[0] == "search")
    [again] = mail.read(conn, imported_db, mailbox)
    assert again.get("messages", 0) == 0 and conn.execute("SELECT max(mails) FROM mail_jobs").fetchone()[0] == 1
    mailbox.messages.append(message("jobalerts-noreply@linkedin.com", "QA Team Lead at Acme Cloud", DIGEST, "<a2@li>"))
    mail.read(conn, imported_db, mailbox)
    assert conn.execute("SELECT mails FROM mail_jobs WHERE identity='linkedin:4470000001'").fetchone()[0] == 2


def test_a_wrong_app_password_is_logged_in_plain_words(conn, imported_db, mailbox):
    mailbox.password = "something else"
    [r] = mail.read(conn, imported_db, mailbox)
    assert not r["ok"] and "app password" in r["error"]
    assert mail.last_reads(conn)[0]["ok"] == 0


def test_your_rules_decide_what_is_shown(conn, imported_db, mailbox):
    mail.read(conn, imported_db, mailbox)
    v = mail.views(conn)
    shown = {x.job["title"]: x for x in v["shown"]}
    held = {x.job["title"]: x.held[0] for x in v["held"]}
    assert set(shown) == {"QA Team Lead", "ראש צוות QA"} and v["shown"][0].level == "FOCUS"
    assert held == {"Hardware Validation Team Leader": "title", "QA Automation Lead (Req 12345)": "known",
                    "Senior QA Engineer": "outside"}


def test_adding_a_job_from_an_alert_puts_it_in_your_tracker(imported_db, mailbox):
    client = TestClient(create_app(imported_db, allowed_hosts=("testserver",), mail_imap=mailbox))
    settings = client.get("/opportunities/settings").text
    assert "test.person@gmail.com" in settings and "abcdabcd" not in settings      # the password is never shown
    assert client.post("/mail/read", headers=SAME, follow_redirects=False).status_code == 303
    page = client.get("/opportunities?tab=mail").text
    assert re.search(r"From alerts <b>2</b>", page) and "QA Team Lead" in page and "Kept off by your rules" in page
    conn = db.connect(imported_db)
    job_id = conn.execute("SELECT id FROM mail_jobs WHERE identity='linkedin:4470000001'").fetchone()[0]
    conn.close()
    assert f'id="mail-{job_id}"' in page
    r = client.post(f"/mail/{job_id}/decision", headers=SAME, data={"decision": "APPLIED"}, follow_redirects=False)
    assert r.status_code == 303
    conn = db.connect(imported_db)
    [a] = added.load(conn)
    assert (a.company, a.role, a.status) == ("Acme Cloud", "QA Team Lead", "APPLIED")
    assert "linkedin:4470000001" in {k for k, hits in pipeline.history_index(conn).by_identity.items()
                                     if any(h.sheet == added.SHEET for h in hits)}
    conn.close()
    page = client.get("/opportunities?tab=mail").text
    assert re.search(r"From alerts <b>1</b>", page) and "Added to your tracker" in page


def test_a_job_you_applied_to_before_is_not_marked_not_a_fit(imported_db, mailbox):
    client = TestClient(create_app(imported_db, allowed_hosts=("testserver",), mail_imap=mailbox))
    client.post("/mail/read", headers=SAME)
    conn = db.connect(imported_db)
    job_id = conn.execute("SELECT id FROM mail_jobs WHERE identity='linkedin:4470000001'").fetchone()[0]
    conn.close()
    assert "Already applied</button>" in client.get("/opportunities?tab=mail").text
    client.post(f"/mail/{job_id}/decision", headers=SAME, data={"decision": "already_applied"})
    conn = db.connect(imported_db)
    v = mail.views(conn)
    [done] = [x for x in v["decided"] if x.job["id"] == job_id]
    assert done.decision == "already_applied" and job_id not in {x.job["id"] for x in v["shown"]}
    assert added.load(conn) == []                       # nothing new in your tracker
    conn.close()
    page = client.get("/opportunities?tab=mail").text
    assert "Already applied — not again" in page and "Not a fit</span>" not in page
    client.post(f"/mail/{job_id}/decision", headers=SAME, data={"decision": "restored"})
    conn = db.connect(imported_db)
    assert job_id in {x.job["id"] for x in mail.views(conn)["shown"]}
    conn.close()


def test_a_mailbox_is_connected_and_removed_in_settings(imported_db):
    client = TestClient(create_app(imported_db, allowed_hosts=("testserver",)))
    bad = client.post("/opportunities/settings/mail", headers=SAME, data={"address": "nope", "password": "x"})
    assert bad.status_code == 400 and "full e-mail address" in bad.text
    client.post("/opportunities/settings/mail", headers=SAME, data={"address": "me@gmail.com", "password": "aaaa bbbb cccc dddd"})
    assert mail.load_config(mail.config_path(imported_db))["mailboxes"] == [("me@gmail.com", "aaaabbbbccccdddd")]
    client.post("/opportunities/settings/mail", headers=SAME, data={"remove": "me@gmail.com"})
    assert mail.load_config(mail.config_path(imported_db))["mailboxes"] == []


def test_easy_apply_is_noticed():
    jobs = {j.title: j.easy_apply for j in mail.parse_linkedin(DIGEST)}
    assert jobs == {"QA Team Lead": True, "Hardware Validation Team Leader": False,
                    "QA Automation Lead (Req 12345)": False, "Senior QA Engineer": False}


def test_the_employer_page_comes_first_on_the_card(imported_db, mailbox):
    client = TestClient(create_app(imported_db, allowed_hosts=("testserver",), mail_imap=mailbox))
    client.post("/mail/read", headers=SAME)
    page = client.get("/opportunities?tab=mail").text
    card = page[page.index("QA Team Lead") - 600:page.index("QA Team Lead") + 2500]
    assert card.index("Find on Acme Cloud") < card.index("LinkedIn ↗")      # the employer's site before LinkedIn
    assert "google.com/search" in card and "-site%3Alinkedin.com" in card
    assert "Easy Apply — employer page first" in card and "Recruiters ↗" in card


def test_employers_in_the_alerts_get_their_boards_checked_once(conn, imported_db, mailbox):
    mail.read(conn, imported_db, mailbox)
    calls = []
    def get(url):
        calls.append(url)
        if "greenhouse" in url and "initech" in url:
            return b'{"jobs": [{"id": 1, "title": "QA Lead", "location": {"name": "Tel Aviv, Israel"}, "absolute_url": "https://boards.greenhouse.io/initech/jobs/1", "first_published": "2026-10-10T00:00:00Z", "updated_at": "2026-10-10T00:00:00Z"}]}'
        raise sources.SourceError("HTTP 404", 404)
    found = mail.probe_boards(conn, get, sleep=lambda s: None)
    added = {f.company for f in found if f.israel_jobs}
    assert added == {"Initech"}
    assert conn.execute("SELECT origin FROM search_sources WHERE board='initech'").fetchone()[0].startswith("found: a company in your alert")
    assert "acme cloud" not in {c.casefold() for c in (f.company for f in found)}    # already on the boards: not probed
    before = len(calls)
    mail.probe_boards(conn, get, sleep=lambda s: None)
    assert len(calls) == before                                                     # each company once


CONFIRMATION = """Your application was sent to Acme Cloud

QA Team Lead
Acme Cloud
Tel Aviv-Yafo
View job: https://www.linkedin.com/comm/jobs/view/4470000001/?trackingId=abc

---------------------------------------------------------

Applied on October 20, 2026----------------------------------------------------------------
        Now, take these next steps for more success

View similar jobs you may be interested in

QA Automation Engineer
Initech
Herzliya
View job: https://www.linkedin.com/comm/jobs/view/4470000077/?trackingId=def

---------------------------------------------------------
"""


def test_a_linkedin_confirmation_puts_the_job_you_applied_to_in_your_tracker(conn, imported_db):
    raw = message("LinkedIn <jobs-noreply@linkedin.com>", "Dana, your application was sent to Acme Cloud", CONFIRMATION, "<c1@li>")
    counts = mail.store(conn, "test.person@gmail.com", [raw])
    assert counts["applications"] == 1
    [a] = added.load(conn)
    assert (a.company, a.role, a.status) == ("Acme Cloud", "QA Team Lead", "APPLIED") and "confirmation e-mail" in a.note
    v = mail.views(conn)
    assert [x.job["title"] for x in v["shown"]] == ["QA Automation Engineer"]          # the similar job, as usual
    assert [x.decision for x in v["decided"]] == ["added"]                              # not shown as new
    again = message("LinkedIn <jobs-noreply@linkedin.com>", "Dana, your application was sent to Acme Cloud", CONFIRMATION, "<c2@li>")
    assert mail.store(conn, "test.person@gmail.com", [again]).get("applications", 0) == 0   # once is enough
    assert len(added.load(conn)) == 1


def test_a_confirmation_for_a_job_you_already_put_in_your_tracker_adds_nothing(conn):
    first = message("LinkedIn <jobs-noreply@linkedin.com>", "QA Team Lead at Acme Cloud", DIGEST, "<a1@li>")
    mail.store(conn, "test.person@gmail.com", [first])
    job_id = conn.execute("SELECT id FROM mail_jobs WHERE identity='linkedin:4470000001'").fetchone()[0]
    mail.record_decision(conn, job_id, "added", "MT 2 · the same job under another number")
    raw = message("LinkedIn <jobs-noreply@linkedin.com>", "Dana, your application was sent to Acme Cloud", CONFIRMATION, "<c1@li>")
    assert mail.store(conn, "test.person@gmail.com", [raw]).get("applications", 0) == 0
    assert added.load(conn) == []


def test_jobs_from_alerts_count_as_new_discoveries(imported_db, mailbox):
    """A quiet day on the career sites is not "nothing new": the main page and the empty Verified Jobs point to the alerts."""
    client = TestClient(create_app(imported_db, allowed_hosts=("testserver",), mail_imap=mailbox))
    client.post("/mail/read", headers=SAME)
    shown = len(mail.views(db.connect(imported_db))["shown"])
    assert shown
    home = client.get("/pipeline").text
    assert f"0 from career sites · {shown} from alerts" in home and 'href="/opportunities?tab=mail"' in home
    verified = client.get("/opportunities?tab=verified").text
    assert f"{shown} new job{'' if shown == 1 else 's'} from your alert e-mails" in verified
