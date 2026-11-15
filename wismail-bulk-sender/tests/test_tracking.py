import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

import core


SECRET = "tracking-test-secret-with-at-least-32-characters"


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "tracking.sqlite3")
        core.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_signed_tokens_reject_tampering_and_local_targets(self):
        token = core.tracking_token(15, "c", SECRET, "https://example.com/path?q=1")
        self.assertEqual(core.verify_tracking_token(token, "c", SECRET), (15, "https://example.com/path?q=1"))
        with self.assertRaises(ValueError):
            core.verify_tracking_token(token, "o", SECRET)
        with self.assertRaises(ValueError):
            core.verify_tracking_token(token[:-1] + ("0" if token[-1] != "0" else "1"), "c", SECRET)
        for target in ("javascript:alert(1)", "http://127.0.0.1/admin", "http://localhost/",
                       "https://example.com\r\nBad: x", "https://example.com/中文"):
            with self.assertRaises(ValueError):
                core.tracking_token(15, "c", SECRET, target)

    def test_html_rewrites_only_http_links_and_adds_pixel(self):
        settings = {"OAUTH_BASE_URL": "https://mail.example.test", "URL_PREFIX": "/bulk", "SESSION_SECRET": SECRET}
        content = '<html><body><a href="https://example.com/?a=1&amp;b=2">Visit</a><a href="mailto:sales@example.com">Reply</a></body></html>'
        result = core.tracked_html(content, 7, settings, True, True)
        self.assertIn("https://mail.example.test/bulk/track/c/", result)
        self.assertIn("https://mail.example.test/bulk/track/o/", result)
        self.assertIn('href="mailto:sales@example.com"', result)
        self.assertLess(result.index("/track/o/"), result.index("</body>"))

    def test_smtp_html_uses_signed_tracking_links_when_enabled(self):
        settings = {"OAUTH_BASE_URL": "https://mail.example.test", "URL_PREFIX": "/bulk", "SESSION_SECRET": SECRET,
                    "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "465", "SMTP_USER": "sender@example.test",
                    "SMTP_PASSWORD": "secret", "SMTP_FROM": "sender@example.test", "MIN_INTERVAL_SECONDS": "1",
                    "DAILY_LIMIT": "100", "HOURLY_LIMIT": "20", "UTC_OFFSET_HOURS": "8"}
        batch = core.create_draft(self.path, "tracked", [(1, "buyer@example.test", "Hello", "Plain",
                           '<a href="https://example.com/offer">Offer</a>')])
        core.schedule_batch(self.path, batch, "now", 60, track_open=True, track_click=True)
        with core.connect(self.path) as db:
            message_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (batch,)).fetchone()[0]
        with mock.patch.object(core.smtplib, "SMTP_SSL") as smtp_class:
            smtp_class.return_value.send_message.return_value = {}
            self.assertTrue(core.dispatch_one(self.path, settings))
        message = smtp_class.return_value.send_message.call_args[0][0]
        parts = list(message.iter_parts())
        self.assertIn("Plain", parts[0].get_content())
        self.assertIn("/bulk/track/c/", parts[1].get_content())
        self.assertIn("/bulk/track/o/", parts[1].get_content())
        token = re.search(r"/track/c/([A-Za-z0-9_-]+\.[0-9a-f]{64})", parts[1].get_content()).group(1)
        self.assertEqual(core.verify_tracking_token(token, "c", SECRET), (message_id, "https://example.com/offer"))

    def test_first_events_feed_behavior_filters_once(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com"})
        core.save_contact(self.path, owner, {"email": "other@example.com"})
        batch = core.create_draft(self.path, "tracked", [(1, "buyer@example.com", "Hi", "Text", "<p>HTML</p>")], owner)
        core.schedule_batch(self.path, batch, "now", 60, track_open=True, track_click=True)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?", (core.iso(core.utcnow()), batch))
            message_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (batch,)).fetchone()[0]
        self.assertTrue(core.record_tracking_event(self.path, message_id, "o"))
        self.assertTrue(core.record_tracking_event(self.path, message_id, "o"))
        self.assertTrue(core.record_tracking_event(self.path, message_id, "c"))
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_events WHERE kind IN ('open','click')").fetchone()[0], 2)
        for key in ("opened_days", "clicked_days"):
            rows = core.select_campaign_contacts(self.path, owner, {key: "30"})
            self.assertEqual([row["email"] for row in rows], ["buyer@example.com"])
        core.save_contact(self.path, "other-owner@example.com", {"email": "buyer@example.com"})
        with self.assertRaisesRegex(ValueError, "筛选结果为空"):
            core.select_campaign_contacts(self.path, "other-owner@example.com", {"opened_days": "30"})

    def test_automation_tags_only_owned_contact_on_first_observed_events(self):
        owner, other = "sender@example.com", "other@example.com"
        for account in (owner, other):
            core.save_contact(self.path, account, {"email": "buyer@example.com"})
        core.save_automation_rule(self.path, owner, "open", "已打开")
        core.save_automation_rule(self.path, owner, "click", "已点击")
        core.save_automation_rule(self.path, other, "open", "其他账号分类")
        batch = core.create_draft(self.path, "tracked", [(1, "buyer@example.com", "Hi", "Text", "<p>HTML</p>")], owner)
        core.schedule_batch(self.path, batch, "now", 60, track_open=True, track_click=True)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?",
                       (core.iso(core.utcnow()), batch))
            message_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (batch,)).fetchone()[0]
        for kind in ("o", "o", "c", "c"):
            self.assertTrue(core.record_tracking_event(self.path, message_id, kind))
        with core.connect(self.path) as db:
            rows = db.execute("""SELECT c.owner_email,g.name FROM contact_category_links l
                                 JOIN contacts c ON c.id=l.contact_id JOIN contact_categories g ON g.id=l.category_id
                                 ORDER BY c.owner_email,g.name""").fetchall()
            self.assertEqual([tuple(row) for row in rows], [(owner, "已打开"), (owner, "已点击")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0], 2)

    def test_unsubscribe_rule_tags_once_and_keeps_send_block(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com"})
        rule = core.save_automation_rule(self.path, owner, "unsubscribe", "已退订")
        batch = core.create_draft(self.path, "queued", [(1, "buyer@example.com", "Hi", "Text")], owner)
        core.schedule_batch(self.path, batch, "now", 60)
        core.add_suppression(self.path, "buyer@example.com", "收件人退订", owner)
        core.add_suppression(self.path, "buyer@example.com", "收件人退订", owner)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM contacts WHERE owner_email=?", (owner,)).fetchone()[0], "unsubscribed")
            self.assertEqual(db.execute("SELECT status FROM messages WHERE batch_id=?", (batch,)).fetchone()[0], "suppressed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM automation_runs WHERE rule_id=?", (rule,)).fetchone()[0], 1)
            category = db.execute("""SELECT g.name FROM contact_category_links l
                                     JOIN contact_categories g ON g.id=l.category_id""").fetchone()[0]
            self.assertEqual(category, "已退订")

    def test_paused_rule_does_not_replay_old_click(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com"})
        rule = core.save_automation_rule(self.path, owner, "click", "新点击")
        core.set_automation_rule(self.path, owner, rule, False)
        batch = core.create_draft(self.path, "tracked", [
            (1, "buyer@example.com", "Hi", "Text", "<p>HTML</p>"),
            (2, "buyer@example.com", "Hi", "Text", "<p>HTML</p>")], owner)
        core.schedule_batch(self.path, batch, "now", 60, track_click=True)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?",
                       (core.iso(core.utcnow()), batch))
            first, second = [row[0] for row in db.execute("SELECT id FROM messages WHERE batch_id=? ORDER BY row_number", (batch,))]
        self.assertTrue(core.record_tracking_event(self.path, first, "c"))
        core.set_automation_rule(self.path, owner, rule, True)
        self.assertTrue(core.record_tracking_event(self.path, first, "c"))
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0], 0)
        self.assertTrue(core.record_tracking_event(self.path, second, "c"))
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0], 1)

    def test_plain_text_batch_cannot_claim_tracking(self):
        batch = core.create_draft(self.path, "plain", [(1, "buyer@example.com", "Hi", "Text")])
        with self.assertRaisesRegex(ValueError, "HTML"):
            core.schedule_batch(self.path, batch, "now", 60, track_open=True)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM batches WHERE id=?", (batch,)).fetchone()[0], "draft")

    def test_existing_batch_table_migrates_without_losing_rows(self):
        old_path = os.path.join(self.temp.name, "old.sqlite3")
        with sqlite3.connect(old_path) as db:
            db.execute("""CREATE TABLE batches (id INTEGER PRIMARY KEY, owner_email TEXT NOT NULL,
                name TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
                start_at TEXT, interval_seconds INTEGER)""")
            db.execute("INSERT INTO batches(id,owner_email,name,state,created_at) VALUES (1,'owner@example.com','old','draft','2026-01-01T00:00:00+00:00')")
        core.init_db(old_path)
        with core.connect(old_path) as db:
            row = db.execute("SELECT name,track_open,track_click FROM batches WHERE id=1").fetchone()
        self.assertEqual(tuple(row), ("old", 0, 0))


if __name__ == "__main__":
    unittest.main()
