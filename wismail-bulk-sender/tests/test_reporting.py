import datetime as dt
import os
import smtplib
import tempfile
import unittest
from unittest import mock

import core


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "report.sqlite3")
        core.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_report_uses_local_event_dates_and_owner_scope(self):
        own = core.create_draft(self.path, "德国", [(1, "a@example.com", "A", "Body")], "owner@example.com")
        failed = core.create_draft(self.path, "失败", [(1, "b@example.com", "B", "Body")], "owner@example.com")
        other = core.create_draft(self.path, "他人", [(1, "c@example.com", "C", "Body")], "other@example.com")
        with core.connect(self.path) as db:
            db.execute("""UPDATE messages SET status='submitted',sender_email='sales@example.com',submitted_at=?,opened_at=?,clicked_at=?,bounced_at=?,replied_at=?
                          WHERE batch_id=?""", ("2026-11-14T16:30:00+00:00", "2026-11-15T01:00:00+00:00",
                                                  "2026-11-15T01:30:00+00:00", "2026-11-15T02:00:00+00:00",
                                                  "2026-11-15T02:30:00+00:00", own))
            db.execute("UPDATE messages SET status='failed',sender_email='team@example.com',last_attempt_at=? WHERE batch_id=?",
                       ("2026-11-15T03:00:00+00:00", failed))
            db.execute("UPDATE messages SET status='submitted',sender_email='foreign@example.com',submitted_at=? WHERE batch_id=?",
                       ("2026-11-15T04:00:00+00:00", other))
        data = core.report_data(self.path, "owner@example.com", dt.date(2026, 11, 15), dt.date(2026, 11, 15))
        self.assertEqual(data["totals"], {"submitted": 1, "opened": 1, "clicked": 1,
                                          "failed": 1, "bounced": 1, "replied": 1})
        self.assertEqual(data["days"][0][0], "2026-11-15")
        self.assertEqual({row["name"] for row in data["batches"]}, {"德国", "失败"})
        self.assertEqual({row["name"]: (row["submitted"], row["failed"], row["bounced"])
                          for row in data["senders"]},
                         {"sales@example.com": (1, 0, 1), "team@example.com": (0, 1, 0)})
        self.assertEqual(core.report_data(self.path, "owner@example.com", dt.date(2026, 11, 14),
                                          dt.date(2026, 11, 14))["totals"]["submitted"], 0)

    def test_dispatch_records_attempt_and_smtp_failure_without_secret(self):
        batch = core.create_draft(self.path, "失败", [(1, "a@example.com", "A", "Body")])
        core.schedule_batch(self.path, batch, "now", 60)
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587", "SMTP_USER": "sender@example.test",
                    "SMTP_PASSWORD": "private-secret", "SMTP_FROM": "sender@example.test",
                    "MIN_INTERVAL_SECONDS": "1", "DAILY_LIMIT": "100", "HOURLY_LIMIT": "20", "UTC_OFFSET_HOURS": "8"}
        def reject(*_):
            raise smtplib.SMTPResponseException(550, b"recipient rejected private-secret")
        with mock.patch.object(core, "_smtp_submit", side_effect=reject):
            self.assertTrue(core.dispatch_one(self.path, settings))
        with core.connect(self.path) as db:
            events = db.execute("SELECT kind,smtp_code,detail FROM delivery_events ORDER BY id").fetchall()
            status = db.execute("SELECT status FROM messages").fetchone()[0]
        self.assertEqual(status, "failed")
        self.assertEqual([row["kind"] for row in events], ["attempt", "failed"])
        self.assertEqual(events[1]["smtp_code"], 550)
        self.assertNotIn("private-secret", events[1]["detail"])


if __name__ == "__main__":
    unittest.main()
