import datetime as dt
import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock

import core
import server
from cryptography.fernet import Fernet
from template_xlsx import build_template


SETTINGS = {
    "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587", "SMTP_USER": "sender@example.test",
    "SMTP_PASSWORD": "test-secret", "SMTP_FROM": "sender@example.test",
    "MIN_INTERVAL_SECONDS": "1", "DAILY_LIMIT": "100", "UTC_OFFSET_HOURS": "8",
}


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "test.sqlite3")
        core.init_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_template_roundtrip_with_chinese_and_multiline_body(self):
        original = build_template()
        source = zipfile.ZipFile(io.BytesIO(original))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data = data.replace(b"</sheetData>", '''<row r="2"><c r="A2" t="inlineStr"><is><t>buyer@example.com</t></is></c><c r="B2" t="inlineStr"><is><t>产品报价</t></is></c><c r="C2" t="inlineStr"><is><t>您好，\n请查收报价。</t></is></c></row></sheetData>'''.encode("utf-8"))
                target.writestr(item, data)
        rows = core.parse_upload("mail.xlsx", output.getvalue())
        self.assertEqual(rows, [(2, "buyer@example.com", "产品报价", "您好，\n请查收报价。")])

    def test_rejects_duplicates_and_unfilled_placeholders(self):
        data = "收件邮箱,邮件主题,邮件正文\nbuyer@example.com,报价,您好\nbuyer@example.com,报价,您好\nother@example.com,询盘,您好{{客户名}}\n".encode("utf-8")
        with self.assertRaises(ValueError) as error:
            core.parse_upload("mail.csv", data)
        self.assertIn("第 3 行", str(error.exception))
        self.assertIn("第 4 行", str(error.exception))

    def test_schedules_submits_once_and_completes(self):
        batch_id = core.create_draft(self.db_path, "报价跟进", [(2, "buyer@example.com", "报价", "您好")])
        local = (core.utcnow() + dt.timedelta(hours=8, minutes=2)).strftime("%Y-%m-%dT%H:%M")
        core.schedule_batch(self.db_path, batch_id, local, 60)
        with core.connect(self.db_path) as db:
            db.execute("UPDATE messages SET next_attempt_at=? WHERE batch_id=?", (core.iso(core.utcnow() - dt.timedelta(seconds=5)), batch_id))
        with mock.patch.object(core, "_smtp_submit", return_value="<test@example.test>") as submit:
            self.assertTrue(core.dispatch_one(self.db_path, SETTINGS))
            self.assertFalse(core.dispatch_one(self.db_path, SETTINGS))
            submit.assert_called_once()
        with core.connect(self.db_path) as db:
            row = db.execute("SELECT status,submitted_at,message_id FROM messages WHERE batch_id=?", (batch_id,)).fetchone()
            self.assertEqual(row["status"], "submitted")
            self.assertTrue(row["submitted_at"])
            self.assertEqual(row["message_id"], "<test@example.test>")
        # A later scheduler pass closes the batch without sending again.
        with mock.patch.object(core, "_smtp_submit") as submit:
            core.dispatch_one(self.db_path, SETTINGS)
            submit.assert_not_called()
        with core.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()["state"], "completed")

    def test_restart_leaves_uncertain_send_for_manual_review(self):
        batch_id = core.create_draft(self.db_path, "重启检查", [(2, "buyer@example.com", "主题", "正文")])
        with core.connect(self.db_path) as db:
            db.execute("UPDATE messages SET status='sending' WHERE batch_id=?", (batch_id,))
        core.init_db(self.db_path)
        with core.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT status FROM messages WHERE batch_id=?", (batch_id,)).fetchone()["status"], "needs_review")

    def test_dashboard_escapes_uploaded_content(self):
        old_path = server.DB_PATH
        server.DB_PATH = self.db_path
        try:
            core.create_draft(self.db_path, '<script>alert(1)</script>', [(2, "buyer@example.com", "报价", "正文")])
            page = server.dashboard()
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;", page)
        finally:
            server.DB_PATH = old_path

    def test_suppression_blocks_import_and_pending_mail(self):
        core.add_suppression(self.db_path, "blocked@example.com")
        with self.assertRaises(ValueError):
            core.create_draft(self.db_path, "不可导入", [(2, "blocked@example.com", "主题", "正文")])
        batch_id = core.create_draft(self.db_path, "等待发送", [(2, "buyer@example.com", "主题", "正文")])
        local = (core.utcnow() + dt.timedelta(hours=8, minutes=2)).strftime("%Y-%m-%dT%H:%M")
        core.schedule_batch(self.db_path, batch_id, local, 60)
        core.add_suppression(self.db_path, "BUYER@example.com")
        with mock.patch.object(core, "_smtp_submit") as submit:
            self.assertFalse(core.dispatch_one(self.db_path, SETTINGS))
            submit.assert_not_called()
        with core.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT status FROM messages WHERE batch_id=?", (batch_id,)).fetchone()["status"], "suppressed")

    def test_export_values_cannot_become_spreadsheet_formulas(self):
        self.assertEqual(server.csv_safe("=HYPERLINK(\"bad\")"), "'=HYPERLINK(\"bad\")")
        self.assertEqual(server.csv_safe("报价"), "报价")

    def test_imap_reply_is_correlated_by_message_id(self):
        batch_id = core.create_draft(self.db_path, "回复识别", [(2, "buyer@example.com", "报价", "正文")])
        with core.connect(self.db_path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=?,message_id=? WHERE batch_id=?",
                       (core.iso(core.utcnow()), "<sent-123@example.com>", batch_id))

        class FakeImap:
            def login(self, *_):
                return "OK", []

            def select(self, *_args, **_kwargs):
                return "OK", []

            def uid(self, command, *_args):
                if command == "SEARCH":
                    return "OK", [b"15"]
                raw = ("From: buyer@example.com\r\nTo: sender@example.com\r\n"
                       "Subject: Re: 报价\r\nIn-Reply-To: <sent-123@example.com>\r\n\r\n谢谢").encode("utf-8")
                return "OK", [(b"15 (RFC822)", raw)]

            def logout(self):
                return "BYE", []

        imap_settings = {"IMAP_HOST": "imap.example.com", "IMAP_USER": "sender@example.com", "IMAP_PASSWORD": "test"}
        with mock.patch.object(core.imaplib, "IMAP4_SSL", return_value=FakeImap()):
            self.assertEqual(core.poll_imap(self.db_path, imap_settings), 1)
        with core.connect(self.db_path) as db:
            self.assertTrue(db.execute("SELECT replied_at FROM messages WHERE batch_id=?", (batch_id,)).fetchone()["replied_at"])

    def test_smtp_submission_uses_tls_and_one_recipient(self):
        class FakeSmtp:
            def __init__(self):
                self.tls = False
                self.login_args = None
                self.to_addrs = None

            def ehlo(self):
                return 250, b"hello"

            def starttls(self, **_kwargs):
                self.tls = True

            def login(self, *args):
                self.login_args = args

            def send_message(self, message, from_addr, to_addrs):
                self.to_addrs = to_addrs
                self.asserted_message = message
                return {}

            def quit(self):
                return 221, b"bye"

        fake = FakeSmtp()
        row = {"recipient_email": "buyer@example.com", "subject": "报价", "body": "完整正文"}
        with mock.patch.object(core.smtplib, "SMTP", return_value=fake):
            message_id = core._smtp_submit(row, SETTINGS)
        self.assertTrue(fake.tls)
        self.assertEqual(fake.login_args, ("sender@example.test", "test-secret"))
        self.assertEqual(fake.to_addrs, ["buyer@example.com"])
        self.assertEqual(fake.asserted_message["Message-ID"], message_id)

    def test_per_mailbox_credentials_are_encrypted_and_isolated(self):
        key = Fernet.generate_key().decode("ascii")
        core.save_mailbox_auth(self.db_path, "alice@example.com", "alice-secret", "imap-secret", key)
        with core.connect(self.db_path) as db:
            ciphertext = db.execute("SELECT smtp_password_enc FROM mailbox_auth WHERE owner_email='alice@example.com'").fetchone()[0]
            self.assertNotIn("alice-secret", ciphertext)
        settings = dict(SETTINGS, CREDENTIAL_KEY=key)
        account = core.mailbox_settings(self.db_path, "alice@example.com", settings)
        self.assertEqual(account["SMTP_USER"], "alice@example.com")
        self.assertEqual(account["SMTP_FROM"], "alice@example.com")
        self.assertEqual(account["SMTP_PASSWORD"], "alice-secret")
        self.assertIsNone(core.mailbox_settings(self.db_path, "bob@example.com", settings))

        alice_batch = core.create_draft(self.db_path, "Alice", [(2, "buyer@example.com", "A", "A")], "alice@example.com")
        old_path = server.DB_PATH
        server.DB_PATH = self.db_path
        try:
            self.assertIsNone(server.batch_page(alice_batch, user="bob@example.com"))
            self.assertIsNotNone(server.batch_page(alice_batch, user="alice@example.com"))
        finally:
            server.DB_PATH = old_path
        core.revoke_mailbox_auth(self.db_path, "alice@example.com")
        self.assertIsNone(core.mailbox_settings(self.db_path, "alice@example.com", settings))

    def test_existing_v1_database_is_migrated(self):
        old_path = os.path.join(self.temp.name, "old.sqlite3")
        with sqlite3.connect(old_path) as db:
            db.execute("CREATE TABLE batches (id INTEGER PRIMARY KEY,name TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,start_at TEXT,interval_seconds INTEGER)")
            db.execute("INSERT INTO batches(id,name,state,created_at) VALUES (1,'旧批次','draft','2026-11-10T00:00:00+00:00')")
            db.execute("CREATE TABLE suppressions (recipient_email TEXT PRIMARY KEY,reason TEXT NOT NULL,created_at TEXT NOT NULL)")
            db.execute("INSERT INTO suppressions VALUES ('blocked@example.com','旧名单','2026-11-10T00:00:00+00:00')")
        core.init_db(old_path)
        with core.connect(old_path) as db:
            self.assertEqual(db.execute("SELECT owner_email FROM batches WHERE id=1").fetchone()[0], "__legacy_admin__")
            self.assertEqual(db.execute("SELECT owner_email FROM suppressions").fetchone()[0], "*")

    def test_worker_uses_each_mailbox_authorization(self):
        key = Fernet.generate_key().decode("ascii")
        settings = dict(SETTINGS, CREDENTIAL_KEY=key, MIN_INTERVAL_SECONDS="60")
        for owner in ("alice@example.com", "bob@example.com"):
            core.save_mailbox_auth(self.db_path, owner, owner + "-secret", "", key)
            batch_id = core.create_draft(self.db_path, owner, [(2, "buyer@example.com", "报价", "正文")], owner)
            local = (core.utcnow() + dt.timedelta(hours=8, minutes=2)).strftime("%Y-%m-%dT%H:%M")
            core.schedule_batch(self.db_path, batch_id, local, 60)
        with core.connect(self.db_path) as db:
            db.execute("UPDATE messages SET next_attempt_at=?", (core.iso(core.utcnow() - dt.timedelta(seconds=5)),))
        used = []

        def fake_submit(row, account):
            used.append((row["owner_email"], account["SMTP_USER"], account["SMTP_PASSWORD"]))
            return "<{}@example.com>".format(row["owner_email"].split("@")[0])

        with mock.patch.object(core, "_smtp_submit", side_effect=fake_submit):
            self.assertTrue(core.dispatch_one(self.db_path, settings))
            self.assertTrue(core.dispatch_one(self.db_path, settings))
        self.assertEqual(used, [
            ("alice@example.com", "alice@example.com", "alice@example.com-secret"),
            ("bob@example.com", "bob@example.com", "bob@example.com-secret"),
        ])


if __name__ == "__main__":
    unittest.main()
