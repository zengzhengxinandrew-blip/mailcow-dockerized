import os
import re
import datetime as dt
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

    def test_device_classification_does_not_keep_raw_user_agent(self):
        self.assertEqual(core.tracking_device("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"), "mobile")
        self.assertEqual(core.tracking_device("Mozilla/5.0 (iPad; CPU OS 17_0)"), "tablet")
        self.assertEqual(core.tracking_device("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), "desktop")
        self.assertEqual(core.tracking_device("GoogleImageProxy"), "unknown")
        self.assertEqual(core.tracking_device(""), "unknown")

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

    def test_behavior_rule_creates_one_owned_draft_without_sending(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "Buyer"})
        template_id, _ = core.save_template(self.path, owner, "跟进", "您好 {{姓名}}", "谢谢 {{姓名}}")
        rule = core.save_automation_rule(self.path, owner, "click", "已点击", template_id)
        source = core.create_draft(self.path, "source", [(1, "buyer@example.com", "原主题", "原正文", "<p>原正文</p>")], owner)
        core.schedule_batch(self.path, source, "now", 60, track_click=True)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted' WHERE batch_id=?", (source,))
            message_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (source,)).fetchone()[0]
        with mock.patch.object(core, "_smtp_submit") as submit:
            self.assertTrue(core.record_tracking_event(self.path, message_id, "c"))
            self.assertTrue(core.record_tracking_event(self.path, message_id, "c"))
            submit.assert_not_called()
        with core.connect(self.path) as db:
            result = db.execute("""SELECT d.status,d.batch_id,b.owner_email,b.state,m.subject,m.body,m.status
                                   FROM automation_drafts d JOIN batches b ON b.id=d.batch_id
                                   JOIN messages m ON m.batch_id=b.id WHERE d.rule_id=?""", (rule,)).fetchall()
            self.assertEqual(len(result), 1)
            self.assertEqual((result[0]["status"], result[0]["owner_email"], result[0]["state"],
                              result[0]["subject"], result[0]["body"]),
                             ("draft", owner, "draft", "您好 Buyer", "谢谢 Buyer"))

    def test_followup_rule_rejects_foreign_template_and_unsubscribe(self):
        template_id, _ = core.save_template(self.path, "other@example.com", "私有", "主题", "正文")
        with self.assertRaisesRegex(ValueError, "自己的"):
            core.save_automation_rule(self.path, "owner@example.com", "open", "打开", template_id)
        with self.assertRaisesRegex(ValueError, "退订"):
            core.save_automation_rule(self.path, "other@example.com", "unsubscribe", "退订", template_id)

    def test_suppressed_contact_gets_no_followup_draft(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com"})
        template_id, _ = core.save_template(self.path, owner, "跟进", "主题", "正文")
        rule = core.save_automation_rule(self.path, owner, "open", "已打开", template_id)
        with core.connect(self.path) as db:
            db.execute("INSERT INTO suppressions(owner_email,recipient_email,reason,created_at) VALUES (?,?,?,?)",
                       (owner, "buyer@example.com", "测试", core.iso(core.utcnow())))
            core._apply_automation_event(db, owner, "buyer@example.com", "open", "open:1")
        with core.connect(self.path) as db:
            row = db.execute("SELECT status,batch_id FROM automation_drafts WHERE rule_id=?", (rule,)).fetchone()
            self.assertEqual(tuple(row), ("skipped", None))

    def test_birthday_rule_uses_local_date_and_deduplicates_each_year(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "Buyer",
                                             "birthday": "2000-11-18"})
        template_id, _ = core.save_template(self.path, owner, "生日", "生日快乐 {{姓名}}", "祝好")
        rule = core.save_automation_rule(self.path, owner, "birthday", "生日联系人", template_id)
        before = dt.datetime(2026, 11, 17, 15, 59, tzinfo=dt.timezone.utc)
        due = dt.datetime(2026, 11, 17, 16, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(core.run_birthday_automations(self.path, before, 8), 0)
        self.assertEqual(core.run_birthday_automations(self.path, due, 8), 1)
        self.assertEqual(core.run_birthday_automations(self.path, due, 8), 0)
        with core.connect(self.path) as db:
            row = db.execute("""SELECT b.state,m.subject,m.status FROM automation_drafts d
                                JOIN batches b ON b.id=d.batch_id JOIN messages m ON m.batch_id=b.id
                                WHERE d.rule_id=?""", (rule,)).fetchone()
            self.assertEqual(tuple(row), ("draft", "生日快乐 Buyer", "draft"))
        next_year = dt.datetime(2027, 11, 17, 16, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(core.run_birthday_automations(self.path, next_year, 8), 1)

    def test_birthday_rule_needs_template_and_leap_day_only_occurs_in_leap_year(self):
        owner = "sender@example.com"
        with self.assertRaisesRegex(ValueError, "必须选择"):
            core.save_automation_rule(self.path, owner, "birthday", "生日")
        core.save_contact(self.path, owner, {"email": "leap@example.com", "birthday": "2000-02-29"})
        template_id, _ = core.save_template(self.path, owner, "生日", "主题", "正文")
        core.save_automation_rule(self.path, owner, "birthday", "生日", template_id)
        ordinary = dt.datetime(2027, 2, 28, 4, tzinfo=dt.timezone.utc)
        leap = dt.datetime(2028, 2, 29, 4, tzinfo=dt.timezone.utc)
        self.assertEqual(core.run_birthday_automations(self.path, ordinary, 0), 0)
        self.assertEqual(core.run_birthday_automations(self.path, leap, 0), 1)

    def test_scheduled_rules_target_owned_category_and_deduplicate(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "Buyer", "categories": "客户"})
        core.save_contact(self.path, owner, {"email": "outside@example.com", "categories": "其他"})
        core.save_contact(self.path, "other@example.com", {"email": "private@example.com", "categories": "客户"})
        template_id, _ = core.save_template(self.path, owner, "跟进", "你好 {{姓名}}", "正文")
        rule = core.save_scheduled_rule(self.path, owner, "weekly", "3", "客户", template_id)
        before = dt.datetime(2026, 11, 17, 15, 59, tzinfo=dt.timezone.utc)
        due = dt.datetime(2026, 11, 17, 16, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(core.run_scheduled_automations(self.path, before, 8), 0)
        with mock.patch.object(core, "_smtp_submit") as submit:
            self.assertEqual(core.run_scheduled_automations(self.path, due, 8), 1)
            self.assertEqual(core.run_scheduled_automations(self.path, due, 8), 0)
            submit.assert_not_called()
        with core.connect(self.path) as db:
            row = db.execute("""SELECT b.owner_email,b.state,m.recipient_email,m.subject,m.status
                                FROM scheduled_runs s JOIN batches b ON b.id=s.batch_id
                                JOIN messages m ON m.batch_id=b.id WHERE s.rule_id=?""", (rule,)).fetchone()
            self.assertEqual(tuple(row), (owner, "draft", "buyer@example.com", "你好 Buyer", "draft"))
        core.set_scheduled_rule(self.path, owner, rule, False)
        self.assertEqual(core.run_scheduled_automations(self.path, due + dt.timedelta(days=7), 8), 0)
        core.set_scheduled_rule(self.path, owner, rule, True)
        self.assertEqual(core.run_scheduled_automations(self.path, due + dt.timedelta(days=7), 8), 1)

    def test_holiday_and_monthly_dates_skip_missing_days_and_suppression(self):
        owner = "sender@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com", "categories": "客户"})
        template_id, _ = core.save_template(self.path, owner, "节日", "主题", "正文")
        core.save_scheduled_rule(self.path, owner, "holiday", "02-29", "客户", template_id)
        core.save_scheduled_rule(self.path, owner, "monthly", "31", "客户", template_id)
        self.assertEqual(core.run_scheduled_automations(self.path, dt.datetime(2027, 2, 28, tzinfo=dt.timezone.utc), 0), 0)
        self.assertEqual(core.run_scheduled_automations(self.path, dt.datetime(2028, 2, 29, tzinfo=dt.timezone.utc), 0), 1)
        self.assertEqual(core.run_scheduled_automations(self.path, dt.datetime(2028, 4, 30, tzinfo=dt.timezone.utc), 0), 0)
        with core.connect(self.path) as db:
            db.execute("INSERT INTO suppressions(owner_email,recipient_email,reason,created_at) VALUES (?,?,?,?)",
                       (owner, "buyer@example.com", "测试", core.iso(core.utcnow())))
        self.assertEqual(core.run_scheduled_automations(self.path, dt.datetime(2028, 5, 31, tzinfo=dt.timezone.utc), 0), 1)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM scheduled_runs WHERE occurrence_date='2028-05-31'").fetchone()[0], "skipped")

    def test_scheduled_rule_rejects_foreign_template_category_and_bad_dates(self):
        core.save_contact(self.path, "other@example.com", {"email": "other@example.com", "categories": "私有"})
        template_id, _ = core.save_template(self.path, "other@example.com", "私有", "主题", "正文")
        with self.assertRaises(ValueError):
            core.save_scheduled_rule(self.path, "owner@example.com", "holiday", "02-30", "私有", template_id)
        with self.assertRaisesRegex(ValueError, "已有"):
            core.save_scheduled_rule(self.path, "owner@example.com", "weekly", "1", "私有", template_id)
        core.save_contact(self.path, "owner@example.com", {"email": "buyer@example.com", "categories": "客户"})
        with self.assertRaisesRegex(ValueError, "自己的"):
            core.save_scheduled_rule(self.path, "owner@example.com", "monthly", "1", "客户", template_id)

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
