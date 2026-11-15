import base64
import csv
import http.client
import io
import os
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode
from http.server import HTTPServer

import core
import server


class HttpFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db, self.old_password = server.DB_PATH, server.ADMIN_PASSWORD
        server.DB_PATH = os.path.join(self.temp.name, "app.sqlite3")
        server.ADMIN_PASSWORD = "local-test-password-123"
        core.init_db(server.DB_PATH)
        self.http = HTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_address[1]
        self.auth = "Basic " + base64.b64encode(b"admin:local-test-password-123").decode("ascii")

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        server.DB_PATH, server.ADMIN_PASSWORD = self.old_db, self.old_password
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        all_headers = {"Authorization": self.auth}
        all_headers.update(headers or {})
        conn.request(method, path, body=body, headers=all_headers)
        result = conn.getresponse()
        output = (result.status, dict(result.getheaders()), result.read())
        conn.close()
        return output

    def test_contacts_and_public_unsubscribe(self):
        old_secret = server.SETTINGS["SESSION_SECRET"]
        server.SETTINGS["SESSION_SECRET"] = "a-long-test-secret"
        try:
            body = urlencode({"csrf": server.CSRF_TOKEN, "email": "buyer@example.com", "name": "Buyer"})
            status, _, _ = self.request("POST", "/contacts/add", body,
                                        {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 303)
            status, _, page = self.request("GET", "/contacts")
            self.assertEqual(status, 200)
            self.assertIn(b"buyer@example.com", page)
            token = core.unsubscribe_token("__legacy_admin__", "buyer@example.com", server.SETTINGS["SESSION_SECRET"])
            status, _, _ = self.request("GET", "/unsubscribe?token=" + token)
            self.assertEqual(status, 200)
            status, _, _ = self.request("POST", "/unsubscribe", urlencode({"token": token}),
                                        {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 200)
            with core.connect(server.DB_PATH) as db:
                self.assertEqual(db.execute("SELECT status FROM contacts").fetchone()["status"], "unsubscribed")
        finally:
            server.SETTINGS["SESSION_SECRET"] = old_secret

    def test_template_upload_preview_requires_explicit_start(self):
        status, _, template = self.request("GET", "/template.xlsx")
        self.assertEqual(status, 200)
        self.assertTrue(template.startswith(b"PK"))
        csv_data = "收件邮箱,邮件主题,邮件正文\nbuyer@example.com,报价,您好，这是报价\n".encode("utf-8")
        boundary = "test-boundary-42"
        body = ("--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n"
                "--{b}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n测试批次\r\n"
                "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"mail.csv\"\r\n"
                "Content-Type: text/csv\r\n\r\n").format(b=boundary, csrf=server.CSRF_TOKEN).encode("utf-8")
        body += csv_data + ("\r\n--{}--\r\n".format(boundary)).encode("utf-8")
        status, headers, _ = self.request("POST", "/upload", body, {"Content-Type": "multipart/form-data; boundary=" + boundary})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/batches/1")
        status, _, page = self.request("GET", "/batches/1")
        self.assertEqual(status, 200)
        self.assertIn("确认并开始排队".encode("utf-8"), page)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT status FROM messages").fetchone()["status"], "draft")

    def test_contact_template_campaign_stays_draft(self):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        contact = urlencode({"csrf": server.CSRF_TOKEN, "email": "buyer@example.com", "name": "Buyer", "categories": "德国"})
        self.assertEqual(self.request("POST", "/contacts/add", contact, headers)[0], 303)
        template = urlencode({"csrf": server.CSRF_TOKEN, "name": "问候", "subject": "Hello {{姓名}}",
                              "body_text": "To {{邮箱}}", "body_html": "<p>{{姓名}}</p>"})
        status, result_headers, _ = self.request("POST", "/templates/save", template, headers)
        self.assertEqual(status, 303)
        self.assertEqual(result_headers["Location"], "/templates?id=1")
        status, _, page = self.request("GET", "/templates?id=1&contact_id=1")
        self.assertEqual(status, 200)
        self.assertIn(b"Hello Buyer", page)
        campaign = urlencode({"csrf": server.CSRF_TOKEN, "name": "德国问候", "template_id": "1", "category": "德国"})
        status, result_headers, _ = self.request("POST", "/campaigns/from-contacts", campaign, headers)
        self.assertEqual(status, 303)
        self.assertEqual(result_headers["Location"], "/batches/1")
        with core.connect(server.DB_PATH) as db:
            batch = db.execute("SELECT state FROM batches").fetchone()
            message = db.execute("SELECT row_number,status,subject,body_html FROM messages").fetchone()
        self.assertEqual(batch["state"], "draft")
        self.assertEqual(tuple(message), (1, "draft", "Hello Buyer", "<p>Buyer</p>"))

    def test_copy_batch_route_creates_draft_and_rejects_other_owner(self):
        source = core.create_draft(server.DB_PATH, "旧任务", [(1, "buyer@example.com", "主题", "正文")])
        form = urlencode({"csrf": server.CSRF_TOKEN, "name": "再次发送前检查"})
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, response_headers, _ = self.request("POST", "/batches/{}/copy".format(source), form, headers)
        self.assertEqual((status, response_headers["Location"]), (303, "/batches/2"))
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT state FROM batches WHERE id=2").fetchone()[0], "draft")
        other = core.create_draft(server.DB_PATH, "他人任务", [(1, "other@example.com", "标题", "正文")],
                                  "other@example.com")
        self.assertEqual(self.request("POST", "/batches/{}/copy".format(other), form, headers)[0], 404)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM batches").fetchone()[0], 3)

    def test_start_batch_saves_reply_address_and_rejects_other_domain(self):
        batch = core.create_draft(server.DB_PATH, "回复地址", [(1, "buyer@example.com", "主题", "正文")])
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        account = {"SMTP_HOST": "smtp.example.test", "SMTP_USER": "sender@example.test",
                   "SMTP_PASSWORD": "test-secret", "SMTP_FROM": "sender@example.test"}
        form = {"csrf": server.CSRF_TOKEN, "immediate": "1", "interval": "120",
                "reply_to": "replies@outside.test"}
        with mock.patch.object(server, "mailbox_settings", return_value=account):
            self.assertEqual(self.request("POST", "/batches/{}/start".format(batch),
                                          urlencode(form), headers)[0], 400)
            form["reply_to"] = "replies@example.test"
            status, result_headers, _ = self.request("POST", "/batches/{}/start".format(batch),
                                                     urlencode(form), headers)
        self.assertEqual((status, result_headers["Location"]), (303, "/batches/1"))
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT reply_to,state FROM batches WHERE id=?", (batch,)).fetchone()[:],
                             ("replies@example.test", "active"))

    def test_start_batch_saves_hourly_limit_and_rejects_above_account_limit(self):
        batch = core.create_draft(server.DB_PATH, "限速任务", [(1, "buyer@example.com", "主题", "正文")])
        account = {"SMTP_HOST": "smtp.example.test", "SMTP_USER": "sender@example.test",
                   "SMTP_PASSWORD": "test-secret", "SMTP_FROM": "sender@example.test"}
        form = {"csrf": server.CSRF_TOKEN, "immediate": "1", "interval": "120", "hourly_limit": "21"}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        with mock.patch.object(server, "mailbox_settings", return_value=account):
            status, _, error = self.request("POST", "/batches/{}/start".format(batch), urlencode(form), headers)
            self.assertEqual(status, 400)
            self.assertIn("不能超过邮箱总上限".encode("utf-8"), error)
            form["hourly_limit"] = "2"
            status, response_headers, _ = self.request("POST", "/batches/{}/start".format(batch), urlencode(form), headers)
        self.assertEqual((status, response_headers["Location"]), (303, "/batches/1"))
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT hourly_limit FROM batches WHERE id=?", (batch,)).fetchone()[0], 2)
        status, _, detail = self.request("GET", "/batches/1")
        self.assertEqual(status, 200)
        self.assertIn("本批次每小时上限：2".encode("utf-8"), detail)

    def test_copy_template_route_uses_selected_version(self):
        original, _ = core.save_template(server.DB_PATH, "__legacy_admin__", "原模板", "旧主题", "正文")
        core.save_template(server.DB_PATH, "__legacy_admin__", "原模板", "新主题", "新正文",
                           template_id=original)
        form = urlencode({"csrf": server.CSRF_TOKEN, "template_id": original,
                          "version": "1", "name": "模板副本"})
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, response_headers, _ = self.request("POST", "/templates/copy", form, headers)
        self.assertEqual((status, response_headers["Location"]), (303, "/templates?id=2"))
        self.assertEqual(core.get_template(server.DB_PATH, "__legacy_admin__", 2)["subject"], "旧主题")
        other, _ = core.save_template(server.DB_PATH, "other@example.com", "私有", "主题", "正文")
        form = urlencode({"csrf": server.CSRF_TOKEN, "template_id": other,
                          "version": "1", "name": "越权"})
        self.assertEqual(self.request("POST", "/templates/copy", form, headers)[0], 400)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM templates").fetchone()[0], 3)

    def test_template_editor_asset_and_source_fallback(self):
        status, _, page = self.request("GET", "/templates")
        self.assertEqual(status, 200)
        self.assertIn(b'data-editor-default="visual"', page)
        self.assertIn(b'src="editor.js"', page)
        self.assertIn(b'name="body_html"', page)
        status, headers, script = self.request("GET", "/editor.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn(b"cleanHtml", script)

    def test_report_and_event_exports_use_existing_results(self):
        batch = core.create_draft(server.DB_PATH, "=报价", [(1, "buyer@example.com", "报价", "正文")])
        with core.connect(server.DB_PATH) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?",
                       ("2026-11-15T01:00:00+00:00", batch))
            message_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (batch,)).fetchone()[0]
            db.execute("INSERT INTO delivery_events(message_id,kind,observed_at,detail) VALUES (?,?,?,?)",
                       (message_id, "submitted", "2026-11-15T01:00:00+00:00", "SMTP 已接受"))
        status, _, page = self.request("GET", "/reports?start=2026-11-15&end=2026-11-15")
        self.assertEqual(status, 200)
        self.assertIn("SMTP 已接受".encode("utf-8"), page)
        status, _, report_csv = self.request("GET", "/reports/export.csv?start=2026-11-15&end=2026-11-15")
        self.assertEqual(status, 200)
        self.assertIn("'=报价".encode("utf-8"), report_csv)
        status, _, events_csv = self.request("GET", "/batches/1/events.csv")
        self.assertEqual(status, 200)
        self.assertIn("SMTP 已接受".encode("utf-8"), events_csv)
        other = core.create_draft(server.DB_PATH, "他人", [(1, "other@example.com", "主题", "正文")], "other@example.com")
        self.assertEqual(self.request("GET", "/batches/{}/events.csv".format(other))[0], 404)
        self.assertEqual(self.request("GET", "/reports?start=2026-11-16&end=2026-11-15")[0], 400)

    def test_batch_flow_shows_progress_without_claiming_delivery(self):
        batch = core.create_draft(server.DB_PATH, "发送流程", [
            (number, "buyer{}@example.test".format(number), "主题", "正文") for number in range(1, 5)])
        status, _, draft = self.request("GET", "/batches/{}".format(batch))
        self.assertEqual(status, 200)
        self.assertIn("发送流程".encode("utf-8"), draft)
        self.assertIn(b"0/4", draft)
        self.assertIn("尚未启动".encode("utf-8"), draft)
        now = core.iso(core.utcnow())
        with core.connect(server.DB_PATH) as db:
            db.execute("UPDATE batches SET state='active',start_at=?,interval_seconds=120 WHERE id=?", (now, batch))
            db.execute("UPDATE messages SET status='queued',scheduled_at=? WHERE batch_id=?", (now, batch))
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=? AND row_number=1", (now, batch))
            db.execute("UPDATE messages SET status='failed' WHERE batch_id=? AND row_number=2", (batch,))
            db.execute("UPDATE messages SET status='needs_review' WHERE batch_id=? AND row_number=3", (batch,))
        status, _, active = self.request("GET", "/batches/{}".format(batch))
        self.assertEqual(status, 200)
        self.assertIn(b"3/4", active)
        self.assertIn("需人工核对 1 封".encode("utf-8"), active)
        self.assertIn("当前排队 1 封".encode("utf-8"), active)
        self.assertNotIn("投递成功".encode("utf-8"), active)
        with core.connect(server.DB_PATH) as db:
            db.execute("UPDATE batches SET state='cancelled' WHERE id=?", (batch,))
            db.execute("UPDATE messages SET status='cancelled' WHERE batch_id=? AND status='queued'", (batch,))
        status, _, cancelled = self.request("GET", "/batches/{}".format(batch))
        self.assertEqual(status, 200)
        self.assertIn(b"4/4", cancelled)
        self.assertIn("批次状态：已取消".encode("utf-8"), cancelled)

    def test_campaign_preview_does_not_create_or_send(self):
        owner = "__legacy_admin__"
        for address in ("first@example.com", "second@example.com"):
            core.save_contact(server.DB_PATH, owner, {"email": address, "categories": "德国"})
        first, _ = core.save_template(server.DB_PATH, owner, "A", "Subject A", "Text A")
        second, _ = core.save_template(server.DB_PATH, owner, "B", "Subject B", "Text B")
        form = {"csrf": server.CSRF_TOKEN, "name": "轮流", "template_id": [str(first), str(second)],
                "include_category": "德国", "exclude_sent_days": "0", "mode": "preview"}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _, body = self.request("POST", "/campaigns", urlencode(form, doseq=True), headers)
        self.assertEqual(status, 200)
        self.assertIn("2 位联系人".encode("utf-8"), body)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        form["mode"] = "create"
        status, result_headers, _ = self.request("POST", "/campaigns", urlencode(form, doseq=True), headers)
        self.assertEqual(status, 303)
        self.assertEqual(result_headers["Location"], "/batches/1")
        with core.connect(server.DB_PATH) as db:
            rows = db.execute("SELECT subject,status FROM messages ORDER BY row_number").fetchall()
        self.assertEqual([(row["subject"], row["status"]) for row in rows],
                         [("Subject A", "draft"), ("Subject B", "draft")])

    def test_ab_preview_and_creation_keep_same_groups_without_sending(self):
        owner = "__legacy_admin__"
        for number in range(4):
            core.save_contact(server.DB_PATH, owner, {"email": "buyer{}@example.test".format(number)})
        first, _ = core.save_template(server.DB_PATH, owner, "A", "Subject A", "Text A")
        second, _ = core.save_template(server.DB_PATH, owner, "B", "Subject B", "Text B")
        form = {"csrf": server.CSRF_TOKEN, "name": "A/B 实验", "template_id": [str(first), str(second)],
                "ab_mode": "1", "mode": "preview"}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _, page = self.request("POST", "/campaigns", urlencode(form, doseq=True), headers)
        self.assertEqual(status, 200)
        self.assertIn("A/B 对比".encode("utf-8"), page)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        form["mode"] = "create"
        status, response_headers, _ = self.request("POST", "/campaigns", urlencode(form, doseq=True), headers)
        self.assertEqual((status, response_headers["Location"]), (303, "/batches/1"))
        with core.connect(server.DB_PATH) as db:
            batch = db.execute("SELECT ab_mode,state FROM batches WHERE id=1").fetchone()
            variants = db.execute("SELECT variant,status FROM messages WHERE batch_id=1").fetchall()
        self.assertEqual(tuple(batch), (1, "draft"))
        self.assertEqual(sorted(row["variant"] for row in variants), ["A", "A", "B", "B"])
        self.assertTrue(all(row["status"] == "draft" for row in variants))
        status, _, detail = self.request("GET", "/batches/1")
        self.assertEqual(status, 200)
        self.assertIn("A/B 对比".encode("utf-8"), detail)
        self.assertIn("观测点击率".encode("utf-8"), detail)
        status, _, exported = self.request("GET", "/batches/1/export.csv")
        self.assertEqual(status, 200)
        records = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))
        self.assertEqual(sorted(row["A/B组"] for row in records), ["A", "A", "B", "B"])

    def test_public_tracking_routes_require_valid_token_and_record_once(self):
        old_secret = server.SETTINGS["SESSION_SECRET"]
        server.SETTINGS["SESSION_SECRET"] = "tracking-test-secret-with-at-least-32-characters"
        try:
            batch = core.create_draft(server.DB_PATH, "tracked", [(1, "buyer@example.com", "Hi", "Text", "<p>HTML</p>")])
            core.schedule_batch(server.DB_PATH, batch, "now", 60, track_open=True, track_click=True)
            with core.connect(server.DB_PATH) as db:
                db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?",
                           (core.iso(core.utcnow()), batch))
                message_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (batch,)).fetchone()[0]
            secret = server.SETTINGS["SESSION_SECRET"]
            open_token = core.tracking_token(message_id, "o", secret)
            click_token = core.tracking_token(message_id, "c", secret, "https://example.com/offer")
            def public_get(path):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                conn.request("GET", path)
                response = conn.getresponse()
                result = response.status, dict(response.getheaders()), response.read()
                conn.close()
                return result
            for _ in range(2):
                status, headers, body = public_get("/track/o/" + open_token + ".gif")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "image/gif")
                self.assertTrue(body.startswith(b"GIF"))
            status, headers, _ = public_get("/track/c/" + click_token)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "https://example.com/offer")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            tampered = open_token[:-1] + ("0" if open_token[-1] != "0" else "1")
            self.assertEqual(public_get("/track/o/" + tampered + ".gif")[0], 404)
            with core.connect(server.DB_PATH) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_events WHERE kind='open'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_events WHERE kind='click'").fetchone()[0], 1)
        finally:
            server.SETTINGS["SESSION_SECRET"] = old_secret


if __name__ == "__main__":
    unittest.main()
