"""Notifications and the schedule plan, with fake senders (nothing is ever sent from tests)."""
from datetime import timedelta

import pytest

from app import cli, db, notify
from search import pipeline
from tests.test_search_pipeline import GREENHOUSE, NOW, FakeBoards, gh, run

CFG = {"NOTIFY_CHANNEL": "email", "SMTP_HOST": "smtp.example.invalid", "SMTP_USER": "me@example.invalid",
       "SMTP_PASSWORD": "x", "NOTIFY_EMAIL_TO": "me@example.invalid"}


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, cfg, subject, text):
        if self.fail:
            raise notify.NotifyError("E-mail failed: SMTPAuthenticationError")
        self.sent.append((subject, text))


def new_job_appears(conn, monkeypatch):
    run(conn)  # first check of every board: nothing is "new"
    later = NOW + timedelta(hours=3)
    monkeypatch.setattr(pipeline, "_now", lambda: later)
    run(conn, FakeBoards(greenhouse={"jobs": GREENHOUSE["jobs"] + [gh(1001, "QA Automation Engineer", "Haifa, Israel",
                                                                             published=later.isoformat())]}), now=later)


def test_only_new_verified_jobs_are_sent_once(conn, monkeypatch):
    outbox = Outbox()
    assert notify.notify_new_jobs(conn, CFG, {"email": outbox})["status"] == "nothing new"
    new_job_appears(conn, monkeypatch)
    result = notify.notify_new_jobs(conn, CFG, {"email": outbox})
    assert result == {"status": "sent", "items": 1, "error": None}
    subject, text = outbox.sent[0]
    assert subject == "Career Console: 1 new verified job"
    assert "QA Automation Engineer" in text and "https://boards.greenhouse.io/acme/jobs/1001" in text
    assert "Dana" not in text  # never contacts or notes
    assert notify.notify_new_jobs(conn, CFG, {"email": outbox})["status"] == "nothing new"  # not sent twice
    assert len(outbox.sent) == 1


def test_a_failed_send_is_logged_and_retried_next_time(conn, monkeypatch):
    new_job_appears(conn, monkeypatch)
    failed = notify.notify_new_jobs(conn, CFG, {"email": Outbox(fail=True)})
    assert failed["status"] == "failed" and "SMTPAuthenticationError" in failed["error"]
    row = conn.execute("SELECT status, error FROM notifications").fetchone()
    assert row["status"] == "failed"
    retry = Outbox()
    assert notify.notify_new_jobs(conn, CFG, {"email": retry})["status"] == "sent" and len(retry.sent) == 1


def test_notifications_are_off_without_settings(conn, tmp_path):
    assert notify.load_config(tmp_path / "missing.env") == {}
    assert notify.notify_new_jobs(conn, {}, {"email": Outbox()})["status"] == "off"


def test_config_file_keeps_secrets_with_hash_signs(tmp_path):
    path = tmp_path / "notify.env"
    path.write_text("# comment\nNOTIFY_CHANNEL=telegram\nTELEGRAM_BOT_TOKEN=123:ab#cd\nSMTP_PASSWORD='p#ss word'\n")
    cfg = notify.load_config(path)
    assert cfg["TELEGRAM_BOT_TOKEN"] == "123:ab#cd" and cfg["SMTP_PASSWORD"] == "p#ss word"
    assert notify.channel(cfg) == "telegram"


def test_missing_fields_are_explained_without_sending():
    with pytest.raises(notify.NotifyError, match="SMTP_PASSWORD"):
        notify.send_email({**CFG, "SMTP_PASSWORD": ""}, "s", "t")
    with pytest.raises(notify.NotifyError, match="TELEGRAM_BOT_TOKEN"):
        notify.send_telegram({"NOTIFY_CHANNEL": "telegram"}, "s", "t")


def test_long_digests_are_capped():
    items = [{"company": f"C{i}", "title": "QA", "freshness": "Fresh", "location": "Haifa", "url": f"https://x/{i}"}
             for i in range(20)]
    subject, text = notify.compose(items)
    assert subject == "Career Console: 20 new verified jobs" and "…and 5 more" in text


def test_schedule_plan_runs_the_search_command_at_the_chosen_hours():
    plist, hours = cli.schedule_plist(2, 8, 22)
    assert hours == [8, 10, 12, 14, 16, 18, 20, 22]
    assert plist["ProgramArguments"][-3:] == ["career-console", "search", "--scheduled"]
    assert plist["StartCalendarInterval"][0] == {"Hour": 8, "Minute": 0}
    with pytest.raises(ValueError):
        cli.schedule_plist(0, 8, 22)
