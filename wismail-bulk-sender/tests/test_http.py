import base64
import http.client
import os
import tempfile
import threading
import unittest
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
