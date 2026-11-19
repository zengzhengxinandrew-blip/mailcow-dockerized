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

    def test_report_groups_cross_year_events_by_local_year(self):
        own = core.create_draft(self.path, "跨年", [(1, "a@example.com", "A", "Body"),
                                                    (2, "b@example.com", "B", "Body")], "owner@example.com")
        other = core.create_draft(self.path, "他人", [(1, "c@example.com", "C", "Body")], "other@example.com")
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=? AND row_number=1",
                       ("2025-12-31T15:30:00+00:00", own))
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=? AND row_number=2",
                       ("2025-12-31T16:30:00+00:00", own))
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?",
                       ("2025-12-31T17:00:00+00:00", other))
        data = core.report_data(self.path, "owner@example.com", dt.date(2025, 12, 31),
                                dt.date(2026, 1, 1))
        self.assertEqual(data["totals"]["submitted"], 2)
        self.assertEqual([(year, counts["submitted"]) for year, counts in data["years"]],
                         [("2026", 1), ("2025", 1)])
        self.assertEqual([(day, counts["submitted"]) for day, counts in data["days"]],
                         [("2026-01-01", 1), ("2025-12-31", 1)])

    def test_failure_summary_groups_codes_by_owner_and_event_date(self):
        own = core.create_draft(self.path, "自身", [(1, "buyer@example.com", "主题", "正文")], "owner@example.com")
        other = core.create_draft(self.path, "他人", [(1, "other@example.com", "主题", "正文")], "other@example.com")
        with core.connect(self.path) as db:
            own_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (own,)).fetchone()[0]
            other_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (other,)).fetchone()[0]
            for message_id, kind, code, observed in (
                (own_id, "failed", 535, "2026-11-14T16:30:00+00:00"),
                (own_id, "failed", 535, "2026-11-14T17:00:00+00:00"),
                (own_id, "needs_review", None, "2026-11-14T18:00:00+00:00"),
                (other_id, "failed", 550, "2026-11-14T19:00:00+00:00"),
                (own_id, "failed", 550, "2026-11-13T16:00:00+00:00")):
                db.execute("INSERT INTO delivery_events(message_id,kind,smtp_code,observed_at) VALUES (?,?,?,?)",
                           (message_id, kind, code, observed))
        rows = core.failure_summary(self.path, "owner@example.com", dt.date(2026, 11, 15),
                                    dt.date(2026, 11, 15))
        self.assertEqual({(row["kind"], row["smtp_code"]): row["messages"] for row in rows},
                         {("failed", 535): 1, ("needs_review", None): 1})
        details = core.failure_details(self.path, "owner@example.com", dt.date(2026, 11, 15),
                                       dt.date(2026, 11, 15))
        self.assertEqual(len(details), 2)
        self.assertEqual({(row["kind"], row["smtp_code"]) for row in details},
                         {("failed", 535), ("needs_review", None)})
        self.assertEqual(core.failure_details(self.path, "owner@example.com", dt.date(2026, 11, 15),
                                              dt.date(2026, 11, 15), kind="failed", code=535)[0]["observed_at"],
                         "2026-11-14T17:00:00+00:00")
        self.assertEqual(len(core.failure_details(self.path, "owner@example.com", dt.date(2026, 11, 15),
                                                  dt.date(2026, 11, 15), kind="needs_review", code="none")), 1)
        with self.assertRaises(ValueError):
            core.failure_details(self.path, "owner@example.com", dt.date(2026, 11, 15),
                                 dt.date(2026, 11, 15), kind="submitted")


if __name__ == "__main__":
    unittest.main()
