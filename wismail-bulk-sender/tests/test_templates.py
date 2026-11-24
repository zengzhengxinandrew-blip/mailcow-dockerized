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

    def test_copy_historical_template_is_independent_and_owner_scoped(self):
        owner = "a@example.com"
        original, _ = core.save_template(self.path, owner, "报价", "旧标题", "旧正文", "<p>旧版</p>")
        core.save_template(self.path, owner, "报价", "新标题", "新正文", "<p>新版</p>", template_id=original)
        copied, version = core.copy_template(self.path, owner, original, "报价副本", 1)
        self.assertNotEqual(copied, original)
        self.assertEqual(version, 1)
        self.assertEqual(core.get_template(self.path, owner, copied)["subject"], "旧标题")
        self.assertEqual(core.get_template(self.path, owner, copied)["body_html"], "<p>旧版</p>")
        core.save_template(self.path, owner, "报价副本", "副本修改", "正文", template_id=copied)
        self.assertEqual(core.get_template(self.path, owner, original)["subject"], "新标题")
        with self.assertRaisesRegex(ValueError, "不存在"):
            core.copy_template(self.path, "b@example.com", original, "越权副本", 1)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM templates").fetchone()[0], 2)

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

    def test_ab_assignments_are_balanced_stable_and_saved(self):
        owner = "a@example.com"
        for number in range(7):
            core.save_contact(self.path, owner, {"email": "buyer{}@example.test".format(number)})
        first, _ = core.save_template(self.path, owner, "A", "Subject A", "Text A", "<p>A</p>")
        second, _ = core.save_template(self.path, owner, "B", "Subject B", "Text B", "<p>B</p>")
        filters = {"ab_mode": "1", "name": "实验一"}
        preview = core.prepare_contact_campaign(self.path, owner, [first, second], filters)
        self.assertEqual(preview, core.prepare_contact_campaign(self.path, owner, [first, second], filters))
        self.assertEqual(sorted(row[8] for row in preview), ["A"] * 4 + ["B"] * 3)
        batch = core.create_contact_campaign(self.path, owner, "实验一", [first, second], filters=filters)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT ab_mode FROM batches WHERE id=?", (batch,)).fetchone()[0], 1)
            rows = db.execute("SELECT recipient_email,template_id,variant FROM messages WHERE batch_id=? ORDER BY row_number",
                              (batch,)).fetchall()
        self.assertEqual([(row["recipient_email"], row["variant"]) for row in rows],
                         [(row[1], row[8]) for row in preview])
        self.assertTrue(all(row["template_id"] == (first if row["variant"] == "A" else second)
                            for row in rows))
        with self.assertRaisesRegex(ValueError, "恰好选择 2"):
            core.prepare_contact_campaign(self.path, owner, [first], filters)

    def test_ab_click_guidance_waits_for_data_and_is_conservative(self):
        now = core.utcnow()
        old = core.iso(now - dt.timedelta(hours=25))
        recent = core.iso(now - dt.timedelta(hours=2))
        strong = [{"submitted": 50, "clicked": 30}, {"submitted": 50, "clicked": 5}]
        self.assertIsNone(core.ab_click_guidance(strong, "active", True, old, now)[0])
        self.assertIsNone(core.ab_click_guidance(strong, "completed", True, recent, now)[0])
        self.assertIsNone(core.ab_click_guidance(strong, "completed", False, old, now)[0])
        self.assertEqual(core.ab_click_guidance(strong, "completed", True, old, now)[0], "A")
        close = [{"submitted": 50, "clicked": 10}, {"submitted": 50, "clicked": 8}]
        self.assertIsNone(core.ab_click_guidance(close, "completed", True, old, now)[0])
        small = [{"submitted": 20, "clicked": 15}, {"submitted": 20, "clicked": 1}]
        self.assertIsNone(core.ab_click_guidance(small, "completed", True, old, now)[0])

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

    def test_html_preview_is_sandboxed_and_escapes_outer_page(self):
        old = server.DB_PATH
        server.DB_PATH = self.path
        try:
            owner = "a@example.com"
            contact_id = core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "Buyer"})
            template_id, _ = core.save_template(self.path, owner, "HTML", "Hello", "Plain",
                                                '<p>Buyer</p><script>alert(1)</script><img src="https://example.com/a.png">')
            page = server.templates_page(owner, selected_id=template_id, preview_contact_id=contact_id)
            self.assertIn('sandbox=""', page)
            self.assertIn('referrerpolicy="no-referrer"', page)
            self.assertIn("Content-Security-Policy", page)
            self.assertIn("img-src data:", page)
            self.assertNotIn("<script>", page)
            self.assertIn("&lt;script&gt;", page)
        finally:
            server.DB_PATH = old

    def test_template_preflight_checks_rendered_content_without_sending(self):
        owner = "a@example.com"
        contact_id = core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "Buyer"})
        with core.connect(self.path) as db:
            contact = db.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        template_id, _ = core.save_template(self.path, owner, "检查", "Hello {{姓名}}", "Body for {{姓名}}",
            '<p>Buyer</p><a href="javascript:alert(1)">Bad</a><a href="http://example.com">Open</a>'
            '<img src="https://example.com/a.png"><script>alert(1)</script>')
        with mock.patch.object(core, "_smtp_submit") as submit:
            errors, warnings = core.preflight_template(core.get_template(self.path, owner, template_id), contact)
            submit.assert_not_called()
        self.assertTrue(any("script" in item for item in errors))
        self.assertTrue(any("链接" in item for item in errors))
        self.assertTrue(any("非 HTTPS" in item for item in warnings))
        self.assertTrue(any("替代文字" in item for item in warnings))
        active, missing_alt = core.preflight_template({"subject": "Hi", "body_text": "Body", "body_html":
            '<p onclick="alert(1)">Text</p><img src="https://example.com/a.png" alt>'}, contact)
        self.assertTrue(any("交互事件" in item for item in active))
        self.assertTrue(any("替代文字" in item for item in missing_alt))
        missing, _ = core.preflight_template({"subject": "Hi {{地区}}", "body_text": "Body", "body_html": ""}, contact)
        self.assertTrue(any("地区" in item for item in missing))
        clean, notes = core.preflight_template({"subject": "Hi {{姓名}}", "body_text": "Body", "body_html":
            '<p>Body</p><a href="https://example.com">Site</a>'}, contact)
        self.assertEqual((clean, notes), ([], []))

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

    def test_test_send_uses_requested_historical_version(self):
        owner = "__legacy_admin__"
        contact_id = core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "Buyer"})
        template_id, _ = core.save_template(self.path, owner, "T", "Old {{姓名}}", "Old body")
        core.save_template(self.path, owner, "T", "New {{姓名}}", "New body", template_id=template_id)
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587", "SMTP_USER": "sender@example.test",
                    "SMTP_PASSWORD": "test-secret", "SMTP_FROM": "sender@example.test"}
        with mock.patch.object(core, "_smtp_submit", return_value="<old@example.test>") as submit:
            core.send_template_test(self.path, owner, template_id, contact_id, "sender@example.test", settings, 1)
        self.assertEqual(submit.call_args[0][0]["subject"], "[测试] Old Buyer")

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
        self.assertNotIn("/unsubscribe?token=", parts[0].get_content())
        self.assertIn("<p>Rich</p>", parts[1].get_content())
        self.assertIn(">Unsubscribe</a>", parts[1].get_content())
        self.assertIn("List-Unsubscribe", message)
        self.assertEqual(message["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_plain_mail_shows_unsubscribe_button_without_long_url(self):
        row = {"owner_email": "sender@example.test", "recipient_email": "buyer@example.test",
               "subject": "Hello", "body": "First line\n<second line>"}
        settings = {"SMTP_HOST": "smtp.example.test", "SMTP_PORT": "465", "SMTP_USER": "sender@example.test",
                    "SMTP_PASSWORD": "secret", "SMTP_FROM": "sender@example.test",
                    "OAUTH_BASE_URL": "https://mail.example.test", "URL_PREFIX": "/bulk",
                    "SESSION_SECRET": "test-secret"}
        with mock.patch.object(core.smtplib, "SMTP_SSL") as smtp_class:
            smtp_class.return_value.send_message.return_value = {}
            core._smtp_submit(row, settings)
        message = smtp_class.return_value.send_message.call_args[0][0]
        plain, rich = list(message.iter_parts())
        self.assertEqual(plain.get_content_type(), "text/plain")
        self.assertEqual(plain.get_content(), "First line\n<second line>\n")
        self.assertEqual(rich.get_content_type(), "text/html")
        self.assertIn("First line<br>", rich.get_content())
        self.assertIn("&lt;second line&gt;", rich.get_content())
        self.assertIn('>Unsubscribe</a>', rich.get_content())
        self.assertIn('/unsubscribe?token=', message["List-Unsubscribe"])
        self.assertEqual(message["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

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
                             "opened_at", "clicked_at", "opened_device", "clicked_device", "variant"}.issubset(columns))


if __name__ == "__main__":
    unittest.main()
