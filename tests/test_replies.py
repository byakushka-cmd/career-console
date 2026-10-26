"""Replies to your applications (search/replies.py): synthetic e-mails in the hiring systems' own wording."""
from email.message import EmailMessage

import pytest

from app import added, db
from search import mail, replies


def reply(sender, subject, body, mid, date="Wed, 14 Oct 2026 09:33:31 +0000"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"], m["Message-ID"] = sender, subject, date, mid
    m.set_content(body)
    return bytes(m)


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


def status(conn, company):
    return {a.role: a.status for a in added.load(conn) if a.company == company}


@pytest.mark.parametrize("subject, expected", [
    ("Thank you for applying for the Director of QA position at Hooli", ("Hooli", "Director of QA")),
    ("We Got It: Thanks for applying for AI-Driven QA Automation Lead", ("", "AI-Driven QA Automation Lead")),
    ("Thanks for applying to ​Wix​!", ("Wix", "")),
    ("Your application for Senior Principal Quality Lead (Identity) at Initech Networks was received",
     ("Initech Networks", "Senior Principal Quality Lead (Identity)")),
    ("We Got It: Thanks for applying the SQA Automation Team Lead role at Vandelay", ("Vandelay", "SQA Automation Team Lead")),
    ("Thank you for your interest in the Network Automation Engineer role with Cato Networks",
     ("Cato Networks", "Network Automation Engineer")),
    ("We Got It: Thanks for applying for Low-level Quality Lead @Umbrella Security!", ("Umbrella Security", "Low-level Quality Lead")),
    ("Application Status @ Wonka", ("Wonka", "")),
    ("עדכון בדבר סטטוס מועמדותך למשרת מוביל.ת צוות בדיקות מערכת באקמה", ("אקמה", "מוביל.ת צוות בדיקות מערכת")),
    ("הגשת מועמדות בנק אקמה", ("בנק אקמה", "")),          # Adam Total, the bank's hiring system
    ("תודה על הגשת המועמדות!", ("", "")),
])
def test_the_subject_names_the_company_and_the_role(subject, expected):
    assert replies.subject_parts(subject) == expected


def test_a_confirmation_makes_an_interested_record_an_application(conn):
    added.add(conn, "", "Acme Cloud — QA Team Lead", "DISCOVERED", company="Acme Cloud", role="QA Team Lead")
    raw = reply("Acme Cloud <no-reply@acmecloud.comeet-notifications.com>", "We Got It: Thanks for applying for QA Team Lead",
                "Hi Dana, Thank you for applying for a position at Acme Cloud. We've received your application.", "<r1@x>",
                date="Tue, 20 Oct 2099 08:00:00 +0000")
    assert replies.process(conn, "me@gmail.com", [raw]) == {"replies": 1, "changed": 1}
    assert status(conn, "Acme Cloud") == {"QA Team Lead": "APPLIED"}
    assert replies.process(conn, "me@gmail.com", [raw]) == {"replies": 0, "changed": 0}     # each e-mail once


def test_a_no_sets_the_application_to_rejected(conn):
    added.add(conn, "", "Globex — Senior QA Engineer", "APPLIED", company="Globex", role="Senior QA Engineer")
    raw = reply("Globex <no-reply@globex.comeet-notifications.com>",
                "Thank you for applying for the Senior QA Engineer position at Globex",
                "Hi Dana, after careful consideration we've decided not to move forward with your candidacy at this time.",
                "<r2@x>", date="Tue, 20 Oct 2099 08:00:00 +0000")
    replies.process(conn, "me@gmail.com", [raw])
    assert status(conn, "Globex") == {"Senior QA Engineer": "REJECTED"}
    [row] = replies.recent(conn)
    assert (row["kind"], row["record_ref"], row["action"]) == ("rejected", "AD 1", "set to Rejected by them")
    assert row["record_href"] == "/added/1"


def test_your_own_later_update_is_kept(conn):
    """Applied in January, an interview set in March: a no dated February is older than your update."""
    added_id = conn.execute("""INSERT INTO added_records(created_at_utc, url, pasted, company, role, job_ids, location)
                               VALUES ('2026-01-05T08:00:00+00:00', NULL, '', 'Kramerica', 'QA Lead', '', NULL)""").lastrowid
    for when, state in (("2026-01-05T08:00:00+00:00", "APPLIED"), ("2026-03-01T08:00:00+00:00", "INTERVIEW")):
        conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                        VALUES (?, ?, ?, ?, '', 'web')""", (when, added.SUBJECT, added_id, state))
    raw = reply("Kramerica <no-reply@kramerica.comeet-notifications.com>", "Thank you for applying for the QA Lead position at Kramerica",
                "Unfortunately, we have decided to move forward with other candidates.", "<r3@x>",
                date="Sun, 01 Feb 2026 08:00:00 +0000")
    replies.process(conn, "me@gmail.com", [raw])
    assert status(conn, "Kramerica") == {"QA Lead": "INTERVIEW"} and "later update" in replies.recent(conn)[0]["action"]


def test_a_confirmation_from_a_company_not_in_your_tracker_adds_it(conn):
    raw = reply("Vandelay Industries <no-reply@vandelay.comeet-notifications.com>",
                "We Got It: Thanks for applying for QA Automation Lead",
                "Thank you for applying. We've received your application and will reach out if it's a good match.", "<r4@x>")
    replies.process(conn, "me@gmail.com", [raw])
    assert status(conn, "Vandelay Industries") == {"QA Automation Lead": "APPLIED"}


def test_a_conditional_no_and_a_newsletter_are_not_rejections(conn):
    maybe = reply("Pied Piper <no-reply@eu.greenhouse-mail.io>", "Thank you for applying to Pied Piper",
                  "Thank you for your application to the QA Engineer position at Pied Piper. If you're not selected this time, "
                  "keep an eye on our careers page.", "<r5@x>")
    news = reply("Town news <iton@example.org>", "Weekly bulletin 994", "Unfortunately, the pool is closed this week.", "<r6@x>")
    replies.process(conn, "me@gmail.com", [maybe, news])
    assert [(r["kind"], r["company"], r["title"]) for r in replies.recent(conn)] == [("applied", "Pied Piper", "QA Engineer")]


def test_a_reply_you_forwarded_is_read_from_its_original_sender(conn):
    added.add(conn, "", "Paragon — Mobile QA Engineer", "APPLIED", company="Paragon", role="Mobile QA Engineer")
    body = ("---------- Forwarded message ---------\nFrom: Paragon <no-reply@paragon.comeet-notifications.com>\n"
            "Subject: Thank you for applying for the Mobile QA Engineer position at Paragon\n\n"
            "Hi Dana, we've decided not to move forward with your candidacy at this time.")
    raw = reply("Me <me.other@gmail.com>", "Fwd: Thank you for applying for the Mobile QA Engineer position at Paragon", body,
                "<r7@x>", date="Tue, 20 Oct 2099 08:00:00 +0000")
    replies.process(conn, "me@gmail.com", [raw])
    assert status(conn, "Paragon") == {"Mobile QA Engineer": "REJECTED"}


def test_replies_are_read_with_the_alerts(conn, imported_db):
    from tests.test_mail import FakeImap
    added.add(conn, "", "Globex — Senior QA Engineer", "APPLIED", company="Globex", role="Senior QA Engineer")
    mail.save_mailbox(mail.config_path(imported_db), "test.person@gmail.com", "abcd abcd abcd abcd")
    box = FakeImap([reply("Globex <no-reply@globex.comeet-notifications.com>",
                          "Thank you for applying for the Senior QA Engineer position at Globex",
                          "We have decided to move forward with other candidates.", "<r8@x>",
                          date="Tue, 20 Oct 2099 08:00:00 +0000")])
    [r] = mail.read(conn, imported_db, box)
    assert r["ok"] and r["replies"] == 1 and r["changed"] == 1
    assert status(conn, "Globex") == {"Senior QA Engineer": "REJECTED"}
