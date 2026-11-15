import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import datetime as dt

import core
import server


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "templates.sqlite3")
        core.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_versions_and_owner_isolation(self):
        template_id, version = core.save_template(self.path, "a@example.com", "跟进", "您好 {{姓名}}", "正文")
        self.assertEqual(version, 1)
        _, version = core.save_template(self.path, "a@example.com", "跟进", "新版 {{姓名}}", "正文2", template_id=template_id)
        self.assertEqual(version, 2)
        self.assertEqual(core.get_template(self.path, "a@example.com", template_id, 1)["subject"], "您好 {{姓名}}")
        self.assertEqual(core.get_template(self.path, "a@example.com", template_id)["subject"], "新版 {{姓名}}")
        with self.assertRaisesRegex(ValueError, "不存在"):
            core.get_template(self.path, "b@example.com", template_id)

    def test_render_and_contact_campaign_snapshot(self):
        core.save_contact(self.path, "a@example.com", {"email": "buyer@example.com", "name": "<Buyer>", "categories": "德国"})
        core.save_contact(self.path, "a@example.com", {"email": "other@example.com", "name": "Other", "categories": "法国"})
        template_id, _ = core.save_template(self.path, "a@example.com", "报价", "您好 {{姓名}}", "文本 {{邮箱}}", "<p>{{姓名}}</p>")
        batch_id = core.create_contact_campaign(self.path, "a@example.com", "德国批次", template_id, "德国")
        with core.connect(self.path) as db:
            row = db.execute("SELECT * FROM messages WHERE batch_id=?", (batch_id,)).fetchone()
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE batch_id=?", (batch_id,)).fetchone()[0], 1)
        self.assertEqual(row["subject"], "您好 <Buyer>")
        self.assertEqual(row["row_number"], 1)
        self.assertEqual(row["body_html"], "<p>&lt;Buyer&gt;</p>")
        core.save_template(self.path, "a@example.com", "报价", "改变", "新正文", template_id=template_id)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT subject FROM messages WHERE id=?", (row["id"],)).fetchone()[0], "您好 <Buyer>")

    def test_missing_variable_rejects_whole_draft(self):
        core.save_contact(self.path, "a@example.com", {"email": "buyer@example.com"})
        template_id, _ = core.save_template(self.path, "a@example.com", "报价", "您好 {{姓名}}", "正文")
        with self.assertRaisesRegex(ValueError, "变量未填写"):
            core.create_contact_campaign(self.path, "a@example.com", "批次", template_id)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)

    def test_campaign_filters_exclusions_and_literal_email_characters(self):
        owner = "a@example.com"
        for address, category in (("sale_1@example.com", "德国"), ("saleX1@example.com", "德国"),
                                  ("sale_2@example.com", "德国,排除"), ("sale_3@example.com", "德国")):
            core.save_contact(self.path, owner, {"email": address, "categories": category})
        core.save_contact(self.path, "b@example.com", {"email": "sale_4@example.com", "categories": "德国"})
        with core.connect(self.path) as db:
            db.execute("UPDATE contacts SET status='unsubscribed' WHERE owner_email=? AND email=?",
                       (owner, "sale_3@example.com"))
        contacts = core.select_campaign_contacts(self.path, owner,
                                                  {"include_category": "德国", "exclude_category": "排除",
                                                   "email_contains": "sale_", "email_excludes": "nomatch"})
        self.assertEqual([row["email"] for row in contacts], ["sale_1@example.com"])

    def test_recently_submitted_contact_is_excluded(self):
        owner = "a@example.com"
        for address in ("old@example.com", "new@example.com"):
            core.save_contact(self.path, owner, {"email": address})
        batch = core.create_draft(self.path, "older", [(1, "old@example.com", "Hello", "Body")], owner)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?",
                       (core.iso(core.utcnow()), batch))
        contacts = core.select_campaign_contacts(self.path, owner, {"exclude_sent_days": "30"})
        self.assertEqual([row["email"] for row in contacts], ["new@example.com"])

    def test_multiple_templates_rotate_and_keep_snapshot(self):
        owner = "a@example.com"
        for address in ("a@example.test", "b@example.test", "c@example.test"):
            core.save_contact(self.path, owner, {"email": address})
        first, _ = core.save_template(self.path, owner, "模板 A", "主题 A", "正文 A")
        second, _ = core.save_template(self.path, owner, "模板 B", "主题 B", "正文 B")
        batch = core.create_contact_campaign(self.path, owner, "轮流发送", [first, second])
        with core.connect(self.path) as db:
            rows = db.execute("SELECT row_number,subject,template_name,template_version FROM messages WHERE batch_id=? ORDER BY row_number",
                              (batch,)).fetchall()
        self.assertEqual([(row["subject"], row["template_name"]) for row in rows],
                         [("主题 A", "模板 A"), ("主题 B", "模板 B"), ("主题 A", "模板 A")])
        self.assertEqual([row["template_version"] for row in rows], [1, 1, 1])
        core.save_template(self.path, owner, "模板 A", "新版", "新版正文", template_id=first)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT subject FROM messages WHERE batch_id=? ORDER BY row_number LIMIT 1",
                                        (batch,)).fetchone()[0], "主题 A")
        with self.assertRaisesRegex(ValueError, "不存在"):
            core.prepare_contact_campaign(self.path, "b@example.com", [first])

    def test_copy_batch_reuses_content_but_resets_delivery_and_checks_blocks(self):
        owner = "a@example.com"
        original = core.create_draft(self.path, "原任务", [
            (1, "first@example.com", "标题", "正文", "<p>正文</p>", 12, "模板", 3),
            (2, "second@example.com", "标题二", "正文二")], owner)
        core.schedule_batch(self.path, original, "now", 60, track_open=False)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=?,attempt_count=2 WHERE batch_id=? AND row_number=1",
                       (core.iso(core.utcnow()), original))
        copied = core.copy_batch(self.path, owner, original, "复制任务")
        with core.connect(self.path) as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (copied,)).fetchone()
            rows = db.execute("SELECT * FROM messages WHERE batch_id=? ORDER BY row_number", (copied,)).fetchall()
        self.assertEqual((batch["name"], batch["state"], batch["start_at"], batch["track_open"]),
                         ("复制任务", "draft", None, 0))
        self.assertEqual((rows[0]["subject"], rows[0]["body_html"], rows[0]["template_version"]),
                         ("标题", "<p>正文</p>", 3))
        self.assertEqual((rows[0]["status"], rows[0]["submitted_at"], rows[0]["attempt_count"]),
                         ("draft", None, 0))
        with self.assertRaisesRegex(ValueError, "批次不存在"):
            core.copy_batch(self.path, "b@example.com", original, "越权")
        core.add_suppression(self.path, "second@example.com", owner_email=owner)
        with self.assertRaisesRegex(ValueError, "不再联系名单"):
            core.copy_batch(self.path, owner, original, "应被拒绝")
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM batches").fetchone()[0], 2)

    def test_template_page_renders_escaped_content(self):
        old = server.DB_PATH
        server.DB_PATH = self.path
        try:
            template_id, _ = core.save_template(self.path, "a@example.com", "模板", "主题", "<script>alert(1)</script>")
            page = server.templates_page("a@example.com", selected_id=template_id)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script>", page)
        finally:
            server.DB_PATH = old

    def test_test_send_requires_own_address_and_records_result(self):
        owner = "__legacy_admin__"
        contact_id = core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "Buyer"})
        template_id, _ = core.save_template(self.path, owner, "T", "Hello {{姓名}}", "Body")
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587", "SMTP_USER": "sender@example.test",
                    "SMTP_PASSWORD": "test-secret", "SMTP_FROM": "sender@example.test"}
        with mock.patch.object(core, "_smtp_submit", return_value="<test@example.test>") as submit:
            with self.assertRaisesRegex(ValueError, "只能发送"):
                core.send_template_test(self.path, owner, template_id, contact_id, "other@example.test", settings)
            submit.assert_not_called()
            core.send_template_test(self.path, owner, template_id, contact_id, "sender@example.test", settings)
            self.assertEqual(submit.call_args[0][0]["subject"], "[测试] Hello Buyer")
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM template_test_logs").fetchone()[0], "submitted")

    def test_html_mail_has_text_alternative_and_unsubscribe(self):
        row = {"owner_email": "sender@example.test", "recipient_email": "buyer@example.test",
               "subject": "Hello", "body": "Plain", "body_html": "<p>Rich</p>"}
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "465", "SMTP_USER": "sender@example.test",
                    "SMTP_PASSWORD": "secret", "SMTP_FROM": "sender@example.test",
                    "OAUTH_BASE_URL": "https://mail.example.test", "URL_PREFIX": "/bulk",
                    "SESSION_SECRET": "test-secret"}
        with mock.patch.object(core.smtplib, "SMTP_SSL") as smtp_class:
            smtp_class.return_value.send_message.return_value = {}
            core._smtp_submit(row, settings)
        message = smtp_class.return_value.send_message.call_args[0][0]
        parts = list(message.iter_parts())
        self.assertEqual([part.get_content_type() for part in parts], ["text/plain", "text/html"])
        self.assertIn("Plain", parts[0].get_content())
        self.assertIn("<p>Rich</p>", parts[1].get_content())
        self.assertIn("退订", parts[1].get_content())
        self.assertIn("List-Unsubscribe", message)

    def test_hourly_limit_blocks_due_message(self):
        owner = "__legacy_admin__"
        first = core.create_draft(self.path, "first", [(2, "first@example.com", "A", "Body")], owner)
        second = core.create_draft(self.path, "second", [(2, "second@example.com", "B", "Body")], owner)
        start = (core.utcnow() + dt.timedelta(hours=8, minutes=2)).strftime("%Y-%m-%dT%H:%M")
        core.schedule_batch(self.path, first, start, 60)
        core.schedule_batch(self.path, second, start, 60)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?", (core.iso(core.utcnow()), first))
            db.execute("UPDATE messages SET next_attempt_at=? WHERE batch_id=?", (core.iso(core.utcnow()), second))
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587", "SMTP_USER": "sender@example.test",
                    "SMTP_PASSWORD": "test-secret", "SMTP_FROM": "sender@example.test", "HOURLY_LIMIT": "1",
                    "DAILY_LIMIT": "100", "MIN_INTERVAL_SECONDS": "1", "UTC_OFFSET_HOURS": "8"}
        with mock.patch.object(core, "_smtp_submit") as submit:
            self.assertFalse(core.dispatch_one(self.path, settings))
            submit.assert_not_called()

    def test_immediate_schedule_uses_current_utc(self):
        batch_id = core.create_draft(self.path, "now", [(1, "buyer@example.com", "Hi", "Body")])
        before = core.utcnow()
        core.schedule_batch(self.path, batch_id, "now", 60)
        after = core.utcnow()
        with core.connect(self.path) as db:
            batch = db.execute("SELECT start_at FROM batches WHERE id=?", (batch_id,)).fetchone()
            row = db.execute("SELECT row_number,scheduled_at FROM messages WHERE batch_id=?", (batch_id,)).fetchone()
        self.assertLessEqual(before, core.parse_iso(batch["start_at"]))
        self.assertLessEqual(core.parse_iso(batch["start_at"]), after)
        self.assertEqual(row["row_number"], 1)
        self.assertEqual(row["scheduled_at"], batch["start_at"])

    def test_existing_database_gets_html_column(self):
        old_path = os.path.join(self.temp.name, "old.sqlite3")
        with sqlite3.connect(old_path) as db:
            db.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY, batch_id INTEGER, row_number INTEGER,
                recipient_email TEXT, subject TEXT, body TEXT, status TEXT, scheduled_at TEXT,
                next_attempt_at TEXT, attempt_count INTEGER, last_attempt_at TEXT, submitted_at TEXT,
                replied_at TEXT, bounced_at TEXT, message_id TEXT, last_error TEXT)""")
        core.init_db(old_path)
        with core.connect(old_path) as db:
            columns = [row["name"] for row in db.execute("PRAGMA table_info(messages)")]
            self.assertTrue({"body_html", "template_id", "template_name", "template_version",
                             "opened_at", "clicked_at"}.issubset(columns))


if __name__ == "__main__":
    unittest.main()
