import base64
import http.client
import os
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
