import http.client
import os
import re
import tempfile
import threading
import unittest
from http.server import HTTPServer
from unittest import mock
from urllib.parse import urlencode

from cryptography.fernet import Fernet

import core
import local_accounts
import oauth_login
import server


ADMIN = "user01@wiskind.top"
CHILD = "user02@wiskind.top"
PASSWORD = "a-long-independent-wismail-password"


class LocalAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "local.sqlite3")
        core.init_db(self.path)
        local_accounts.init_accounts(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_activation_requires_own_smtp_password_and_keeps_data_isolated(self):
        _, code = local_accounts.create_invite(self.path, CHILD, ADMIN)
        with self.assertRaises(ValueError):
            local_accounts.login(self.path, CHILD, PASSWORD)
        settings = {"SMTP_HOST": "mail.example.test", "SMTP_PORT": "587",
                    "CREDENTIAL_KEY": Fernet.generate_key().decode("ascii")}
        with mock.patch.object(local_accounts, "verify_mailbox_auth", side_effect=ValueError("bad SMTP")):
            with self.assertRaises(ValueError):
                local_accounts.activate_account(self.path, CHILD, code, "wrong", "", PASSWORD, settings)
        with core.connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT owner_email FROM mailbox_auth WHERE owner_email=?", (CHILD,)).fetchone())
        with mock.patch.object(local_accounts, "verify_mailbox_auth") as verify:
            local_accounts.activate_account(self.path, CHILD, code, "child-smtp-secret", "", PASSWORD, settings)
        verify.assert_called_once()
        token = local_accounts.login(self.path, CHILD, PASSWORD)
        self.assertEqual(local_accounts.read_session(self.path, token), CHILD)
        with core.connect(self.path) as db:
            saved = db.execute("SELECT smtp_password_enc FROM mailbox_auth WHERE owner_email=?", (CHILD,)).fetchone()
            self.assertIsNotNone(saved)
            self.assertNotIn("child-smtp-secret", saved[0])
        with self.assertRaises(ValueError):
            local_accounts.activate_account(self.path, CHILD, code, "x", "", PASSWORD, settings)
        local_accounts.disable_account(self.path, CHILD, ADMIN)
        self.assertIsNone(local_accounts.read_session(self.path, token))

    def test_wrong_password_locks_account_and_reset_revokes_session(self):
        _, code = local_accounts.create_invite(self.path, CHILD, ADMIN)
        settings = {"SMTP_HOST": "mail.example.test", "SMTP_PORT": "587",
                    "CREDENTIAL_KEY": Fernet.generate_key().decode("ascii")}
        with mock.patch.object(local_accounts, "verify_mailbox_auth"):
            local_accounts.activate_account(self.path, CHILD, code, "child-smtp-secret", "", PASSWORD, settings)
        token = local_accounts.login(self.path, CHILD, PASSWORD)
        with core.connect(self.path) as db:
            db.execute("INSERT INTO batches(owner_email,name,state,created_at) VALUES (?,?,?,?)",
                       (CHILD, "pending", "active", core.iso(core.utcnow())))
        for _ in range(5):
            with self.assertRaises(ValueError):
                local_accounts.login(self.path, CHILD, "wrong-password")
        with self.assertRaisesRegex(ValueError, "登录尝试过多"):
            local_accounts.login(self.path, CHILD, PASSWORD)
        local_accounts.reset_invite(self.path, CHILD, ADMIN)
        self.assertIsNone(local_accounts.read_session(self.path, token))
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM batches WHERE owner_email=?", (CHILD,)).fetchone()[0],
                             "paused")
        with self.assertRaises(ValueError):
            local_accounts.create_invite(self.path, ADMIN, ADMIN)
        with self.assertRaises(ValueError):
            local_accounts.create_invite(self.path, "someone@example.com", ADMIN)


class LocalAccountHttpTests(LocalAccountTests):
    def setUp(self):
        super().setUp()
        self.old_db, self.old_prefix, self.old_settings = server.DB_PATH, server.URL_PREFIX, dict(server.SETTINGS)
        server.DB_PATH = self.path
        server.URL_PREFIX = "/bulk"
        server.SETTINGS.update({
            "OAUTH_BASE_URL": "https://mail.example.test",
            "OAUTH_CLIENT_ID": "test-client",
            "OAUTH_CLIENT_SECRET": "test-secret",
            "OAUTH_REDIRECT_URI": "https://mail.example.test/bulk/oauth/callback",
            "SESSION_SECRET": "test-session-secret-at-least-32-characters",
            "CREDENTIAL_KEY": Fernet.generate_key().decode("ascii"),
            "TEAM_ADMIN_EMAIL": ADMIN,
            "SMTP_HOST": "mail.example.test",
            "SMTP_PORT": "587",
        })
        self.http = HTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.admin_token = oauth_login.new_session(ADMIN, server.SETTINGS)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        server.DB_PATH, server.URL_PREFIX = self.old_db, self.old_prefix
        server.SETTINGS.clear()
        server.SETTINGS.update(self.old_settings)
        super().tearDown()

    def request(self, method, path, data=None, cookie=""):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=5)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=urlencode(data) if data is not None else None, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode("utf-8")
        connection.close()
        return result

    def test_admin_invites_child_and_child_sees_only_own_data(self):
        admin_cookie = "wismail_session=" + self.admin_token
        self.assertEqual(self.request("GET", "/bulk/static.css")[0], 200)
        self.assertEqual(self.request("GET", "/bulk/team", cookie=admin_cookie)[0], 200)
        csrf = oauth_login.csrf_token(ADMIN, server.SETTINGS)
        status, _, body = self.request("POST", "/bulk/team/create", {"csrf": csrf, "email": CHILD}, admin_cookie)
        self.assertEqual(status, 200)
        code = re.search(r"<code>([^<]+)</code>", body).group(1)
        status, headers, body = self.request("GET", "/bulk/team/activate")
        self.assertEqual(status, 200)
        nonce = re.search(r'name="nonce" value="([^"]+)"', body).group(1)
        nonce_cookie = headers["Set-Cookie"].split(";", 1)[0]
        with mock.patch.object(local_accounts, "verify_mailbox_auth"):
            status, _, _ = self.request("POST", "/bulk/team/activate", {
                "nonce": nonce, "email": CHILD, "invite_code": code,
                "smtp_password": "child-smtp-secret", "password": PASSWORD,
                "password_repeat": PASSWORD}, nonce_cookie)
        self.assertEqual(status, 200)
        status, headers, body = self.request("GET", "/bulk/team/login")
        self.assertEqual(status, 200)
        nonce = re.search(r'name="nonce" value="([^"]+)"', body).group(1)
        nonce_cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, _ = self.request("POST", "/bulk/team/login", {
            "nonce": nonce, "email": CHILD, "password": PASSWORD}, nonce_cookie)
        self.assertEqual(status, 303)
        child_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", "/bulk/team", cookie=child_cookie)[0], 403)
        self.assertEqual(self.request("POST", "/bulk/team/create", {
            "csrf": oauth_login.csrf_token(CHILD, server.SETTINGS), "email": "user03@wiskind.top"},
            child_cookie)[0], 403)
        core.save_contact(self.path, ADMIN, {"email": "admin-contact@example.test"})
        core.save_contact(self.path, CHILD, {"email": "child-contact@example.test"})
        child_contacts = self.request("GET", "/bulk/contacts", cookie=child_cookie)[2]
        admin_contacts = self.request("GET", "/bulk/contacts", cookie=admin_cookie)[2]
        self.assertIn("child-contact@example.test", child_contacts)
        self.assertNotIn("admin-contact@example.test", child_contacts)
        self.assertIn("admin-contact@example.test", admin_contacts)
        self.assertNotIn("child-contact@example.test", admin_contacts)
        self.assertEqual(self.request("GET", "/bulk/logout", cookie=child_cookie)[0], 303)
        self.assertEqual(self.request("GET", "/bulk/", cookie=child_cookie)[0], 303)
        self.assertEqual(self.request("POST", "/bulk/team/disable", {"csrf": csrf, "email": CHILD},
                                      admin_cookie)[0], 200)
        child_oauth = "wismail_session=" + oauth_login.new_session(CHILD, server.SETTINGS)
        self.assertEqual(self.request("GET", "/bulk/", cookie=child_oauth)[0], 403)
