import os
import tempfile
import unittest

import core


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "contacts.sqlite3")
        core.init_db(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_import_merges_without_resubscribing(self):
        data = "邮箱,姓名,分类,变量1\nBuyer@Example.com,李先生,德国客户,工业\n".encode()
        items = core.parse_contacts_upload("contacts.csv", data)
        self.assertEqual(core.import_contacts(self.path, "a@example.com", items), 1)
        with core.connect(self.path) as db:
            row = db.execute("SELECT id,email FROM contacts").fetchone()
        self.assertEqual(row["email"], "buyer@example.com")
        core.set_contact_status(self.path, "a@example.com", row["id"], "unsubscribed")
        core.import_contacts(self.path, "a@example.com", [{"email": row["email"], "name": "新姓名"}])
        with core.connect(self.path) as db:
            saved = db.execute("SELECT name,status FROM contacts WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual((saved["name"], saved["status"]), ("新姓名", "unsubscribed"))

    def test_blocked_contact_rejected_at_import_and_queued_message_suppressed(self):
        contact_id = core.save_contact(self.path, "a@example.com", {"email": "buyer@example.com"})
        batch = core.create_draft(self.path, "test", [(2, "buyer@example.com", "subject", "body")], "a@example.com")
        core.schedule_batch(self.path, batch, "2030-01-01T10:00", 60)
        core.set_contact_status(self.path, "a@example.com", contact_id, "complained")
        with core.connect(self.path) as db:
            state = db.execute("SELECT status FROM messages WHERE batch_id=?", (batch,)).fetchone()["status"]
        self.assertEqual(state, "suppressed")
        with self.assertRaisesRegex(ValueError, "禁止发送"):
            core.create_draft(self.path, "again", [(2, "buyer@example.com", "subject", "body")], "a@example.com")

    def test_contact_isolation_and_recycle(self):
        contact_id = core.save_contact(self.path, "a@example.com", {"email": "buyer@example.com"})
        with self.assertRaisesRegex(ValueError, "不存在"):
            core.set_contact_status(self.path, "b@example.com", contact_id, "invalid")
        core.set_contact_deleted(self.path, "a@example.com", contact_id, True)
        with self.assertRaisesRegex(ValueError, "禁止发送"):
            core.create_draft(self.path, "test", [(2, "buyer@example.com", "subject", "body")], "a@example.com")
        core.set_contact_deleted(self.path, "a@example.com", contact_id, False)
        core.create_draft(self.path, "other", [(2, "buyer@example.com", "subject", "body")], "b@example.com")

    def test_unsubscribe_token_is_scoped_and_signed(self):
        token = core.unsubscribe_token("a@example.com", "buyer@example.com", "long-secret")
        self.assertEqual(core.verify_unsubscribe_token(token, "long-secret"),
                         ("a@example.com", "buyer@example.com"))
        with self.assertRaisesRegex(ValueError, "无效"):
            core.verify_unsubscribe_token(token[:-1] + "0", "long-secret")

    def test_existing_suppression_stays_blocked_after_contact_import(self):
        core.add_suppression(self.path, "buyer@example.com", owner_email="a@example.com")
        core.import_contacts(self.path, "a@example.com", [{"email": "buyer@example.com", "name": "Buyer"}])
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM contacts").fetchone()["status"], "unsubscribed")

    def test_category_management_preserves_status_and_rule_references(self):
        owner = "a@example.com"
        contact_id = core.save_contact(self.path, owner, {"email": "buyer@example.com", "categories": "客户"})
        core.set_contact_status(self.path, owner, contact_id, "unsubscribed")
        category_id = core.create_category(self.path, owner, "重点")
        core.set_contact_category(self.path, owner, contact_id, category_id, True)
        with self.assertRaisesRegex(ValueError, "联系人不存在"):
            core.set_contact_category(self.path, "b@example.com", contact_id, category_id, False)
        core.rename_category(self.path, owner, category_id, "优先")
        with core.connect(self.path) as db:
            row = db.execute("""SELECT c.status,g.name FROM contacts c JOIN contact_category_links l
                                ON l.contact_id=c.id JOIN contact_categories g ON g.id=l.category_id
                                WHERE c.id=? AND g.id=?""", (contact_id, category_id)).fetchone()
            self.assertEqual(tuple(row), ("unsubscribed", "优先"))
        core.set_contact_category(self.path, owner, contact_id, category_id, False)
        core.delete_category(self.path, owner, category_id)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM contacts WHERE id=?", (contact_id,)).fetchone()[0],
                             "unsubscribed")

    def test_category_rename_updates_automations_and_active_category_cannot_be_deleted(self):
        owner = "a@example.com"
        contact_id = core.save_contact(self.path, owner, {"email": "buyer@example.com", "categories": "客户"})
        template_id, _ = core.save_template(self.path, owner, "测试", "主题", "正文")
        core.save_automation_rule(self.path, owner, "click", "客户")
        core.save_scheduled_rule(self.path, owner, "weekly", "1", "客户", template_id)
        with core.connect(self.path) as db:
            category_id = db.execute("SELECT id FROM contact_categories WHERE owner_email=? AND name='客户'",
                                     (owner,)).fetchone()[0]
        core.rename_category(self.path, owner, category_id, "新客户")
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT category_name FROM automation_rules").fetchone()[0], "新客户")
            self.assertEqual(db.execute("SELECT target_category FROM scheduled_rules").fetchone()[0], "新客户")
        with self.assertRaisesRegex(ValueError, "规则"):
            core.delete_category(self.path, owner, category_id)
        with self.assertRaisesRegex(ValueError, "分类不存在"):
            core.rename_category(self.path, "b@example.com", category_id, "外部")


if __name__ == "__main__":
    unittest.main()
