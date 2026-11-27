import datetime as dt
import http.client
import os
import smtplib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

import core
import server


SETTINGS = {
    "SMTP_HOST": "smtp.example.test", "SMTP_PORT": "587",
    "SMTP_USER": "sender@example.test", "SMTP_PASSWORD": "secret",
    "SMTP_FROM": "sender@example.test", "MIN_INTERVAL_SECONDS": "1",
    "DAILY_LIMIT": "100", "HOURLY_LIMIT": "20", "UTC_OFFSET_HOURS": "8",
}


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "wismail.sqlite3")
        core.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def due_batch(self):
        batch = core.create_draft(self.path, "test", [(1, "buyer@example.test", "Subject", "Body")])
        core.schedule_batch(self.path, batch, "now", 60)
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET next_attempt_at=? WHERE batch_id=?",
                       (core.iso(core.utcnow() - dt.timedelta(seconds=5)), batch))
        return batch

    def test_unknown_smtp_result_requires_review_without_retry(self):
        batch = self.due_batch()
        calls = []

        class FakeSMTP:
            def __init__(self, *args, **kwargs): pass
            def ehlo(self): pass
            def starttls(self, **kwargs): pass
            def login(self, *args): pass
            def quit(self): pass
            def send_message(self, *args, **kwargs):
                calls.append("DATA accepted, response lost")
                raise smtplib.SMTPServerDisconnected("lost after DATA")

        with mock.patch.object(core.smtplib, "SMTP", FakeSMTP):
            self.assertTrue(core.dispatch_one(self.path, SETTINGS))
            self.assertFalse(core.dispatch_one(self.path, SETTINGS))
        self.assertEqual(len(calls), 1)
        with core.connect(self.path) as db:
            row = db.execute("SELECT status,next_attempt_at,attempt_count FROM messages WHERE batch_id=?", (batch,)).fetchone()
            self.assertEqual((row["status"], row["next_attempt_at"], row["attempt_count"]),
                             ("needs_review", None, 1))
            self.assertEqual(db.execute("SELECT kind FROM delivery_events ORDER BY id DESC LIMIT 1").fetchone()[0],
                             "needs_review")

    def test_explicit_temporary_smtp_rejection_is_retryable(self):
        batch = self.due_batch()
        with mock.patch.object(core, "_smtp_submit", side_effect=smtplib.SMTPDataError(451, b"try later")):
            self.assertTrue(core.dispatch_one(self.path, SETTINGS))
        with core.connect(self.path) as db:
            row = db.execute("SELECT status,next_attempt_at FROM messages WHERE batch_id=?", (batch,)).fetchone()
            self.assertEqual(row["status"], "queued")
            self.assertIsNotNone(row["next_attempt_at"])

    def test_imap_failed_uid_is_retried_and_uidvalidity_reset_is_safe(self):
        class FakeImap:
            validity = b"100"
            fail_first = True
            fetches = []

            def __init__(self, *args, **kwargs):
                self.timeout = kwargs.get("timeout")

            def login(self, *args): return "OK", []
            def select(self, *args, **kwargs): return "OK", []
            def response(self, name): return "UIDVALIDITY", [self.validity]
            def logout(self): return "BYE", []

            def uid(self, command, *args):
                if command == "SEARCH":
                    return "OK", [b"1 2"]
                uid = args[0]
                self.fetches.append(uid)
                if uid == "1" and self.fail_first:
                    return "NO", []
                return "OK", [(b"message", b"From: buyer@example.test\r\nSubject: hi\r\n\r\nhello")]

        settings = {"IMAP_HOST": "imap.example.test", "IMAP_USER": "sender@example.test", "IMAP_PASSWORD": "secret"}
        with mock.patch.object(core.imaplib, "IMAP4_SSL", FakeImap):
            self.assertEqual(core.poll_imap(self.path, settings), 0)
            with core.connect(self.path) as db:
                self.assertIsNone(db.execute("SELECT value FROM imap_state WHERE key='last_uid:__legacy_admin__'").fetchone())
            FakeImap.fail_first = False
            self.assertEqual(core.poll_imap(self.path, settings), 2)
            FakeImap.validity = b"101"
            self.assertEqual(core.poll_imap(self.path, settings), 2)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM imap_events").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT value FROM imap_state WHERE key='uidvalidity:__legacy_admin__'").fetchone()[0], "101")
        self.assertEqual(FakeImap.fetches, ["1", "1", "2", "1", "2"])

    def test_imap_limits_each_round_and_resumes_at_next_uid(self):
        class ManyMessages:
            def __init__(self, *args, **kwargs): pass
            def login(self, *args): return "OK", []
            def select(self, *args, **kwargs): return "OK", []
            def response(self, name): return "UIDVALIDITY", [b"500"]
            def logout(self): return "BYE", []
            def uid(self, command, *args):
                if command == "SEARCH":
                    return "OK", [b" ".join(str(i).encode() for i in range(1, 52))]
                return "OK", [(b"message", b"From: buyer@example.test\r\n\r\nhello")]

        settings = {"IMAP_HOST": "imap.example.test", "IMAP_USER": "sender@example.test", "IMAP_PASSWORD": "secret"}
        with mock.patch.object(core.imaplib, "IMAP4_SSL", ManyMessages):
            self.assertEqual(core.poll_imap(self.path, settings), 50)
            self.assertEqual(core.poll_imap(self.path, settings), 1)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM imap_events").fetchone()[0], 51)

    def test_slow_imap_does_not_block_sending_worker(self):
        entered = threading.Event()
        release = threading.Event()
        sent = threading.Event()
        settings = dict(SETTINGS, IMAP_USER="sender@example.test", IMAP_PASSWORD="secret")

        def slow_imap(*args):
            entered.set()
            release.wait(3)

        imap = core.ImapWorker(self.path, settings)
        sender = core.Worker(self.path, settings)
        try:
            with mock.patch.object(core, "poll_imap", side_effect=slow_imap), \
                    mock.patch.object(core, "mailbox_settings", return_value=settings), \
                    mock.patch.object(core, "dispatch_one", side_effect=lambda *args: sent.set()):
                imap.start()
                self.assertTrue(entered.wait(2))
                sender.start()
                self.assertTrue(sent.wait(2))
        finally:
            release.set()
            imap.stop_event.set()
            sender.stop_event.set()
            imap.join(3)
            if sender.ident:
                sender.join(3)

    def test_concurrent_http_serves_fast_request_while_slow_one_waits(self):
        entered = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/slow":
                    entered.set()
                    release.wait(3)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args): pass

        http_server = server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        port = http_server.server_address[1]

        def request(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", path)
            response = conn.getresponse()
            result = response.status, response.read()
            conn.close()
            return result

        slow = threading.Thread(target=request, args=("/slow",), daemon=True)
        try:
            slow.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(request("/fast"), (200, b"ok"))
        finally:
            release.set()
            slow.join(3)
            http_server.shutdown()
            http_server.server_close()
            thread.join(3)

    def test_dashboard_aggregate_matches_message_statuses(self):
        first = core.create_draft(self.path, "first", [(1, "a@example.test", "x", "y"), (2, "b@example.test", "x", "y")])
        second = core.create_draft(self.path, "second", [(1, "c@example.test", "x", "y")])
        with core.connect(self.path) as db:
            db.execute("UPDATE messages SET status='needs_review' WHERE batch_id=? AND row_number=1", (first,))
            db.execute("UPDATE messages SET status='submitted',submitted_at=? WHERE batch_id=?", (core.iso(core.utcnow()), second))
            stats = server.summaries_for(db, [first, second])
        self.assertEqual((stats[first]["total"], stats[first]["failed"], stats[first]["submitted"]), (2, 1, 0))
        self.assertEqual((stats[second]["total"], stats[second]["submitted"]), (1, 1))

    def test_dashboard_uses_one_message_aggregation_for_multiple_batches(self):
        for number in range(4):
            core.create_draft(self.path, "batch {}".format(number),
                              [(1, "buyer{}@example.test".format(number), "x", "y")])
        previous = server.DB_PATH
        server.DB_PATH = self.path
        queries = []

        def traced_connect(path):
            db = core.connect(path)
            db.set_trace_callback(queries.append)
            return db

        try:
            with mock.patch.object(server, "connect", side_effect=traced_connect):
                page = server.dashboard()
            self.assertIn("batch 3", page)
        finally:
            server.DB_PATH = previous
        self.assertEqual(sum("FROM messages" in sql for sql in queries), 1)

    def test_template_list_reads_latest_versions_without_per_template_fetches(self):
        owner = "sender@example.test"
        first, _ = core.save_template(self.path, owner, "First", "Old", "Body")
        core.save_template(self.path, owner, "First", "New", "Body", template_id=first)
        core.save_template(self.path, owner, "Second", "Other", "Body")
        previous = server.DB_PATH
        server.DB_PATH = self.path
        try:
            with mock.patch.object(server, "get_template", side_effect=AssertionError("per-template fetch")):
                page = server.templates_page(owner)
            self.assertIn("First · v2", page)
            self.assertIn("Second · v1", page)
        finally:
            server.DB_PATH = previous


if __name__ == "__main__":
    unittest.main()
