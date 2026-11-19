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

    def test_provider_feedback_upload_requires_csrf_and_records_source(self):
        def upload(token):
            boundary = "wismail-feedback-test"
            parts = []
            for name, value in (("csrf", token), ("source_name", "服务商后台")):
                parts.append(('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}\r\n'
                              .format(boundary, name, value)).encode())
            parts.append(('--{}\r\nContent-Disposition: form-data; name="file"; filename="feedback.csv"\r\n'
                          'Content-Type: text/csv\r\n\r\n'.format(boundary)).encode())
            parts.append('邮箱,类型\nbuyer@example.com,投诉\n'.encode())
            parts.append(('\r\n--{}--\r\n'.format(boundary)).encode())
            return self.request("POST", "/contacts/feedback/import", b"".join(parts),
                                {"Content-Type": "multipart/form-data; boundary=" + boundary})

        self.assertEqual(upload("wrong")[0], 403)
        status, _, page = upload(server.CSRF_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("已导入 1 条反馈".encode(), page)
        self.assertIn("服务商后台".encode(), page)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT status FROM contacts WHERE email='buyer@example.com'").fetchone()[0],
                             "complained")
            self.assertEqual(db.execute("SELECT count(*) FROM provider_feedback_imports").fetchone()[0], 1)
        self.assertIn("无需重复处理".encode(), upload(server.CSRF_TOKEN)[2])

    def test_audit_shows_only_successful_own_actions_without_form_values(self):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        invalid = urlencode({"csrf": "wrong", "email": "private@example.com", "name": "Secret Customer"})
        self.assertEqual(self.request("POST", "/contacts/add", invalid, headers)[0], 403)
        valid = urlencode({"csrf": server.CSRF_TOKEN, "email": "private@example.com", "name": "Secret Customer"})
        self.assertEqual(self.request("POST", "/contacts/add", valid, headers)[0], 303)
        core.record_audit_event(server.DB_PATH, "other@example.com", "/account/revoke")
        status, _, body = self.request("GET", "/audit")
        self.assertEqual(status, 200)
        self.assertIn("添加联系人".encode(), body)
        self.assertNotIn(b"private@example.com", body)
        self.assertNotIn(b"Secret Customer", body)
        self.assertNotIn("撤销发件授权".encode(), body)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events WHERE owner_email='__legacy_admin__'").fetchone()[0], 1)

    def test_notification_settings_and_reads_are_owner_scoped(self):
        own = core.create_draft(server.DB_PATH, "自己的任务", [(1, "own@example.com", "主题", "正文")])
        other = core.create_draft(server.DB_PATH, "他人任务", [(1, "other@example.com", "主题", "正文")], "other@example.com")
        with core.connect(server.DB_PATH) as db:
            own_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (own,)).fetchone()[0]
            other_id = db.execute("SELECT id FROM messages WHERE batch_id=?", (other,)).fetchone()[0]
            event_id = db.execute("INSERT INTO delivery_events(message_id,kind,observed_at) VALUES (?,'failed',?)",
                                  (own_id, core.iso(core.utcnow()))).lastrowid
            foreign_id = db.execute("INSERT INTO delivery_events(message_id,kind,observed_at) VALUES (?,'bounce',?)",
                                    (other_id, core.iso(core.utcnow()))).lastrowid
        status, _, body = self.request("GET", "/notifications")
        self.assertEqual(status, 200)
        self.assertIn("自己的任务".encode(), body)
        self.assertNotIn("他人任务".encode(), body)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.request("POST", "/notifications/read",
                                      urlencode({"csrf": server.CSRF_TOKEN, "id": foreign_id}), headers)[0], 400)
        self.assertEqual(self.request("POST", "/notifications/read",
                                      urlencode({"csrf": server.CSRF_TOKEN, "id": event_id}), headers)[0], 303)
        self.assertIn("已读".encode(), self.request("GET", "/notifications")[2])
        self.assertEqual(self.request("POST", "/notifications/preferences",
                                      urlencode({"csrf": server.CSRF_TOKEN, "bounces": "1"}), headers)[0], 303)
        self.assertNotIn("自己的任务".encode(), self.request("GET", "/notifications")[2])
        self.assertEqual(core.notification_preferences(server.DB_PATH, "other@example.com"), (True, True))

    def test_automation_rules_are_owner_scoped_and_do_not_send(self):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        with mock.patch.object(core, "_smtp_submit") as submit:
            form = urlencode({"csrf": server.CSRF_TOKEN, "event_kind": "click", "category_name": "已点击"})
            status, response_headers, _ = self.request("POST", "/automations/add", form, headers)
            self.assertEqual((status, response_headers["Location"]), (303, "/automations"))
            submit.assert_not_called()
        status, _, page = self.request("GET", "/automations")
        self.assertEqual(status, 200)
        self.assertIn("已点击".encode("utf-8"), page)
        self.assertEqual(self.request("POST", "/automations/add", form, headers)[0], 400)
        other_rule = core.save_automation_rule(server.DB_PATH, "other@example.com", "open", "私有分类")
        toggle = urlencode({"csrf": server.CSRF_TOKEN, "id": other_rule, "enabled": "0"})
        self.assertEqual(self.request("POST", "/automations/toggle", toggle, headers)[0], 400)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT enabled FROM automation_rules WHERE id=?", (other_rule,)).fetchone()[0], 1)
        own_toggle = urlencode({"csrf": server.CSRF_TOKEN, "id": "1", "enabled": "0"})
        self.assertEqual(self.request("POST", "/automations/toggle", own_toggle, headers)[0], 303)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT enabled FROM automation_rules WHERE id=1").fetchone()[0], 0)
        remove = urlencode({"csrf": server.CSRF_TOKEN, "id": "1"})
        self.assertEqual(self.request("POST", "/automations/delete", remove, headers)[0], 303)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM automation_rules WHERE owner_email='__legacy_admin__'").fetchone()[0], 0)

    def test_automation_form_accepts_owned_followup_template_only(self):
        template_id, _ = core.save_template(server.DB_PATH, "__legacy_admin__", "跟进", "主题", "正文")
        foreign_id, _ = core.save_template(server.DB_PATH, "other@example.com", "私有", "主题", "正文")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        form = urlencode({"csrf": server.CSRF_TOKEN, "event_kind": "open", "category_name": "已打开",
                          "followup_template_id": str(template_id)})
        self.assertEqual(self.request("POST", "/automations/add", form, headers)[0], 303)
        status, _, page = self.request("GET", "/automations")
        self.assertEqual(status, 200)
        self.assertIn("跟进".encode("utf-8"), page)
        self.assertNotIn("私有".encode("utf-8"), page)
        invalid = urlencode({"csrf": server.CSRF_TOKEN, "event_kind": "click", "category_name": "已点击",
                             "followup_template_id": str(foreign_id)})
        self.assertEqual(self.request("POST", "/automations/add", invalid, headers)[0], 400)

    def test_scheduled_rule_form_is_owner_scoped(self):
        owner = "__legacy_admin__"
        core.save_contact(server.DB_PATH, owner, {"email": "buyer@example.com", "categories": "客户"})
        template_id, _ = core.save_template(server.DB_PATH, owner, "循环模板", "主题", "正文")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        form = urlencode({"csrf": server.CSRF_TOKEN, "kind": "weekly", "schedule_value": "1",
                          "target_category": "客户", "template_id": str(template_id)})
        status, response_headers, _ = self.request("POST", "/automations/scheduled/add", form, headers)
        self.assertEqual((status, response_headers["Location"]), (303, "/automations"))
        status, _, page = self.request("GET", "/automations")
        self.assertEqual(status, 200)
        self.assertIn("循环模板".encode("utf-8"), page)
        self.assertEqual(self.request("POST", "/automations/scheduled/add", form, headers)[0], 400)
        core.save_contact(server.DB_PATH, "other@example.com", {"email": "private@example.com", "categories": "私有"})
        private_template, _ = core.save_template(server.DB_PATH, "other@example.com", "私有模板", "主题", "正文")
        other_id = core.save_scheduled_rule(server.DB_PATH, "other@example.com", "weekly", "2", "私有", private_template)
        self.assertEqual(self.request("POST", "/automations/scheduled/delete",
                                      urlencode({"csrf": server.CSRF_TOKEN, "id": other_id}), headers)[0], 400)
        with core.connect(server.DB_PATH) as db:
            own_id = db.execute("SELECT id FROM scheduled_rules WHERE owner_email=?", (owner,)).fetchone()[0]
        self.assertEqual(self.request("POST", "/automations/scheduled/toggle",
                                      urlencode({"csrf": server.CSRF_TOKEN, "id": own_id, "enabled": "0"}), headers)[0], 303)
        with core.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT enabled FROM scheduled_rules WHERE id=?", (own_id,)).fetchone()[0], 0)

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

    def test_historical_template_precheck_and_test_use_displayed_version(self):
        owner = "__legacy_admin__"
        contact_id = core.save_contact(server.DB_PATH, owner, {"email": "buyer@example.com", "name": "Buyer"})
        template_id, _ = core.save_template(server.DB_PATH, owner, "报价", "Old {{姓名}}", "Old body",
                                            '<p>Old</p><a href="javascript:alert(1)">Bad</a>')
        core.save_template(server.DB_PATH, owner, "报价", "New {{姓名}}", "New body", template_id=template_id)
        path = "/templates?id={}&version=1&contact_id={}&check=1".format(template_id, contact_id)
        with mock.patch.object(core, "_smtp_submit") as submit:
            status, _, checked = self.request("GET", path)
            submit.assert_not_called()
        self.assertEqual(status, 200)
        self.assertIn("发送前预检测 · v1".encode("utf-8"), checked)
        self.assertIn(b"Old Buyer", checked)
        self.assertIn("不支持的链接".encode("utf-8"), checked)
        self.assertIn(b'name="version" value="1"', checked)
        self.assertIn("当前显示历史版本".encode("utf-8"), checked)
        account = {"SMTP_HOST": "smtp.example.test", "SMTP_USER": "sender@example.test",
                   "SMTP_PASSWORD": "test-secret", "SMTP_FROM": "sender@example.test"}
        form = urlencode({"csrf": server.CSRF_TOKEN, "template_id": template_id, "version": "1",
                          "contact_id": contact_id, "recipient": "sender@example.test", "confirm_test": "1"})
        with mock.patch.object(core, "mailbox_settings", return_value=account), \
                mock.patch.object(core, "_smtp_submit", return_value="<old@example.test>") as submit:
            status, headers, _ = self.request("POST", "/templates/test", form,
                                               {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((status, headers["Location"]), (303, "/templates?id=1&version=1"))
        self.assertEqual(submit.call_args[0][0]["subject"], "[测试] Old Buyer")

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
