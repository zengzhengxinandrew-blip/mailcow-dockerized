import http.client
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.fernet import Fernet

import core
import oauth_login
import server


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = server.DB_PATH
        self.old_settings = server.SETTINGS
        self.old_prefix = server.URL_PREFIX
        server.URL_PREFIX = "/bulk"
        server.DB_PATH = os.path.join(self.temp.name, "oauth.sqlite3")
        server.SETTINGS = dict(server.SETTINGS, OAUTH_BASE_URL="https://mail.example.test",
                               OAUTH_CLIENT_ID="test-client", OAUTH_CLIENT_SECRET="test-secret",
                               OAUTH_REDIRECT_URI="https://mail.wiskind.top/bulk/oauth/callback",
                               SESSION_SECRET="a-long-stable-session-secret-for-tests-12345",
                               CREDENTIAL_KEY=Fernet.generate_key().decode("ascii"))
        core.init_db(server.DB_PATH)
        self.http = HTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_address[1]

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        server.DB_PATH, server.SETTINGS, server.URL_PREFIX = self.old_db, self.old_settings, self.old_prefix
        self.temp.cleanup()

    def request(self, path, cookie=""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Cookie": cookie} if cookie else {}
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return result

    def test_login_reuses_mailcow_identity_and_keeps_batches_private(self):
        status, headers, _ = self.request("/bulk/")
        self.assertEqual((status, headers["Location"]), (303, "/bulk/login"))
        status, headers, _ = self.request("/bulk/login")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("https://mail.example.test/oauth/authorize?"))
        state = parse_qs(urlparse(headers["Location"]).query)["state"][0]
        state_cookie = headers["Set-Cookie"].split(";", 1)[0]
        with mock.patch.object(oauth_login, "exchange_code", return_value="alice@example.com") as exchange:
            status, headers, _ = self.request("/bulk/oauth/callback?code=test-code&state=" + state, state_cookie)
            self.assertEqual((status, headers["Location"]), (303, "/bulk/"))
            exchange.assert_called_once()
        session_cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("/bulk/", session_cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"alice@example.com", body)
        self.assertIn(b'href="/bulk/static.css"', body)
        self.assertIn(b'action="/bulk/upload"', body)
        other_batch = core.create_draft(server.DB_PATH, "Bob only", [(2, "buyer@example.com", "Hi", "Body")], "bob@example.com")
        status, _, _ = self.request("/bulk/batches/{}".format(other_batch), session_cookie)
        self.assertEqual(status, 404)
        status, _, _ = self.request("/bulk/batches/{}/export.csv".format(other_batch), session_cookie)
        self.assertEqual(status, 404)

    def test_state_and_session_are_signed(self):
        state, cookie = oauth_login.new_state(server.SETTINGS)
        self.assertTrue(oauth_login.valid_state(state, cookie, server.SETTINGS))
        self.assertFalse(oauth_login.valid_state("changed", cookie, server.SETTINGS))
        session = oauth_login.new_session("alice@example.com", server.SETTINGS)
        self.assertEqual(oauth_login.read_session(session, server.SETTINGS), "alice@example.com")
        self.assertIsNone(oauth_login.read_session(session + "x", server.SETTINGS))

    def test_mailbox_authorization_can_be_saved_and_revoked(self):
        session = oauth_login.new_session("alice@example.com", server.SETTINGS)
        cookie = "wismail_session=" + session
        status, _, page = self.request("/bulk/account", cookie)
        self.assertEqual(status, 200)
        self.assertIn("alice@example.com", page.decode("utf-8"))
        form = urlencode({"csrf": oauth_login.csrf_token("alice@example.com", server.SETTINGS),
                          "smtp_password": "smtp-app-secret", "imap_password": ""})
        with mock.patch.object(server, "verify_mailbox_auth") as verify:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/bulk/account", body=form,
                         headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.close()
            verify.assert_called_once()
        account = core.mailbox_settings(server.DB_PATH, "alice@example.com", server.SETTINGS)
        self.assertEqual(account["SMTP_PASSWORD"], "smtp-app-secret")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/bulk/account/revoke", body=form,
                     headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        self.assertEqual(response.status, 303)
        response.read()
        conn.close()
        self.assertIsNone(core.mailbox_settings(server.DB_PATH, "alice@example.com", server.SETTINGS))


if __name__ == "__main__":
    unittest.main()
