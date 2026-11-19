import os
import json
import sqlite3
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

    def test_import_deduplicates_complementary_rows_and_preserves_existing_fields(self):
        owner = "a@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com", "name": "旧姓名", "phone": "123"})
        data = ("邮箱,姓名,地区,分类,变量1\n"
                "Buyer@Example.com,,德国,客户,工业\n"
                "buyer@example.com,,,重点,工业\n").encode()
        items = core.parse_contacts_upload("contacts.csv", data)
        self.assertEqual(len(items), 1)
        self.assertEqual(core.import_contacts(self.path, owner, items), 1)
        with core.connect(self.path) as db:
            row = db.execute("SELECT name,phone,region,custom_json FROM contacts WHERE owner_email=?", (owner,)).fetchone()
            categories = [x[0] for x in db.execute("""SELECT g.name FROM contact_category_links l JOIN contact_categories g
                                                       ON g.id=l.category_id JOIN contacts c ON c.id=l.contact_id
                                                       WHERE c.owner_email=? ORDER BY g.name""", (owner,))]
        self.assertEqual((row["name"], row["phone"], row["region"]), ("旧姓名", "123", "德国"))
        self.assertEqual(json.loads(row["custom_json"])["custom_1"], "工业")
        self.assertEqual(categories, ["客户", "重点"])

    def test_conflicting_duplicate_rows_reject_entire_file(self):
        data = "邮箱,姓名\nbuyer@example.com,张先生\nBUYER@example.com,李先生\n".encode()
        with self.assertRaisesRegex(ValueError, "第 3 行与第 2 行"):
            core.parse_contacts_upload("contacts.csv", data)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM contacts").fetchone()[0], 0)

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

    def test_only_own_manual_suppression_can_be_removed_without_resubscribing(self):
        owner = "a@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com"})
        batch = core.create_draft(self.path, "待发", [(1, "buyer@example.com", "主题", "正文")], owner)
        core.schedule_batch(self.path, batch, "now", 60)
        core.add_suppression(self.path, "buyer@example.com", owner_email=owner)
        with self.assertRaises(ValueError):
            core.remove_manual_suppression(self.path, "b@example.com", "buyer@example.com")
        core.remove_manual_suppression(self.path, owner, "buyer@example.com")
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM contacts WHERE owner_email=?", (owner,)).fetchone()[0], "unsubscribed")
            self.assertEqual(db.execute("SELECT status FROM messages WHERE batch_id=?", (batch,)).fetchone()[0], "suppressed")
        core.add_suppression(self.path, "buyer@example.com", "收件人退订", owner)
        core.add_suppression(self.path, "buyer@example.com", owner_email=owner)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT reason FROM suppressions WHERE owner_email=?", (owner,)).fetchone()[0], "收件人退订")
        with self.assertRaises(ValueError):
            core.remove_manual_suppression(self.path, owner, "buyer@example.com")

    def test_provider_feedback_is_atomic_idempotent_and_owner_scoped(self):
        owner = "a@example.com"
        core.save_contact(self.path, owner, {"email": "buyer@example.com"})
        own_batch = core.create_draft(self.path, "待发", [(1, "buyer@example.com", "主题", "正文")], owner)
        other_batch = core.create_draft(self.path, "其他账号", [(1, "buyer@example.com", "主题", "正文")], "b@example.com")
        core.schedule_batch(self.path, own_batch, "now", 60)
        core.schedule_batch(self.path, other_batch, "now", 60)
        bad = "邮箱,类型\nbuyer@example.com,投诉\nwrong-address,无效地址\n".encode()
        with self.assertRaisesRegex(ValueError, "第 3 行"):
            core.import_provider_feedback(self.path, owner, "服务商", "feedback.csv", bad)
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM provider_feedback_imports").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM messages WHERE batch_id=?", (own_batch,)).fetchone()[0], "queued")

        data = "邮箱,类型\nbuyer@example.com,投诉\nmissing@example.com,无效地址\n".encode()
        self.assertEqual(core.import_provider_feedback(self.path, owner, "服务商", "feedback.csv", data), (2, 2, 1))
        self.assertIsNone(core.import_provider_feedback(self.path, owner, "重复导入", "renamed.csv", data))
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM contacts WHERE owner_email=? AND email=?",
                                        (owner, "buyer@example.com")).fetchone()[0], "complained")
            self.assertEqual(db.execute("SELECT status FROM contacts WHERE owner_email=? AND email=?",
                                        (owner, "missing@example.com")).fetchone()[0], "invalid")
            self.assertEqual(db.execute("SELECT status FROM messages WHERE batch_id=?", (own_batch,)).fetchone()[0], "suppressed")
            self.assertEqual(db.execute("SELECT status FROM messages WHERE batch_id=?", (other_batch,)).fetchone()[0], "queued")
            self.assertEqual(db.execute("SELECT count(*) FROM provider_feedback_events").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            core.create_draft(self.path, "再次发送", [(1, "missing@example.com", "主题", "正文")], owner)
        later = "邮箱,类型\nbuyer@example.com,无效地址\n".encode()
        self.assertEqual(core.import_provider_feedback(self.path, owner, "服务商", "later.csv", later), (1, 0, 0))
        with core.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM contacts WHERE owner_email=? AND email=?",
                                        (owner, "buyer@example.com")).fetchone()[0], "complained")

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

    def test_manual_merge_preserves_strict_status_and_stops_both_queues(self):
        owner = "a@example.com"
        primary_id = core.save_contact(self.path, owner, {"email": "first@example.com", "name": "主姓名",
                                                          "categories": "主分类", "custom_1": "保留"})
        secondary_id = core.save_contact(self.path, owner, {"email": "second@example.com", "phone": "12345",
                                                            "categories": "次分类", "custom_2": "补齐"})
        batch = core.create_draft(self.path, "待发", [(1, "first@example.com", "A", "A"),
                                                      (2, "second@example.com", "B", "B")], owner)
        core.schedule_batch(self.path, batch, "2030-01-01T10:00", 60)
        core.set_contact_status(self.path, owner, secondary_id, "complained", "客户投诉")
        # A fresh batch for the still-subscribed primary must also be stopped by the merge.
        another = core.create_draft(self.path, "主地址待发", [(1, "first@example.com", "C", "C")], owner)
        core.schedule_batch(self.path, another, "2030-01-01T10:00", 60)
        self.assertEqual(core.merge_contacts(self.path, owner, "first@example.com", "second@example.com"), primary_id)
        with core.connect(self.path) as db:
            primary = db.execute("SELECT * FROM contacts WHERE id=?", (primary_id,)).fetchone()
            secondary = db.execute("SELECT * FROM contacts WHERE id=?", (secondary_id,)).fetchone()
            labels = [r[0] for r in db.execute("""SELECT g.name FROM contact_category_links l JOIN contact_categories g
                                               ON g.id=l.category_id WHERE l.contact_id=? ORDER BY g.name""", (primary_id,))]
            self.assertEqual((primary["name"], primary["phone"], primary["status"], primary["status_reason"]),
                             ("主姓名", "12345", "complained", "客户投诉"))
            self.assertEqual(labels, ["主分类", "次分类"])
            self.assertEqual(json.loads(primary["custom_json"]), {"custom_1": "保留", "custom_2": "补齐"})
            self.assertEqual(secondary["merged_into_contact_id"], primary_id)
            self.assertIsNotNone(secondary["deleted_at"])
            self.assertEqual([r[0] for r in db.execute("SELECT status FROM messages WHERE batch_id=?", (another,))],
                             ["suppressed"])
        with self.assertRaisesRegex(ValueError, "不能直接恢复"):
            core.set_contact_deleted(self.path, owner, secondary_id, False)
        core.save_contact(self.path, owner, {"email": "second@example.com", "name": "重新导入"})
        with core.connect(self.path) as db:
            self.assertIsNotNone(db.execute("SELECT deleted_at FROM contacts WHERE id=?", (secondary_id,)).fetchone()[0])

    def test_manual_merge_rejects_cross_account_and_archived_contacts(self):
        owner = "a@example.com"
        core.save_contact(self.path, owner, {"email": "first@example.com"})
        secondary_id = core.save_contact(self.path, "b@example.com", {"email": "second@example.com"})
        with self.assertRaisesRegex(ValueError, "当前账号"):
            core.merge_contacts(self.path, owner, "first@example.com", "second@example.com")
        core.set_contact_deleted(self.path, "b@example.com", secondary_id, True)
        core.save_contact(self.path, "b@example.com", {"email": "third@example.com"})
        with self.assertRaisesRegex(ValueError, "当前账号"):
            core.merge_contacts(self.path, "b@example.com", "third@example.com", "second@example.com")

    def test_existing_contact_table_adds_merge_column_without_losing_rows(self):
        old_path = os.path.join(self.temp.name, "old-contacts.sqlite3")
        with sqlite3.connect(old_path) as db:
            db.execute("""CREATE TABLE contacts (id INTEGER PRIMARY KEY,owner_email TEXT NOT NULL,email TEXT NOT NULL,
                         name TEXT NOT NULL DEFAULT '',gender TEXT NOT NULL DEFAULT '',birthday TEXT NOT NULL DEFAULT '',
                         phone TEXT NOT NULL DEFAULT '',region TEXT NOT NULL DEFAULT '',custom_json TEXT NOT NULL DEFAULT '{}',
                         status TEXT NOT NULL DEFAULT 'subscribed',status_reason TEXT NOT NULL DEFAULT '',deleted_at TEXT,
                         created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(owner_email,email))""")
            db.execute("INSERT INTO contacts(owner_email,email,created_at,updated_at) VALUES (?,?,?,?)",
                       ("a@example.com", "buyer@example.com", "2026-01-01", "2026-01-01"))
        core.init_db(old_path)
        with core.connect(old_path) as db:
            self.assertIn("merged_into_contact_id", [r["name"] for r in db.execute("PRAGMA table_info(contacts)")])
            self.assertEqual(db.execute("SELECT email FROM contacts").fetchone()[0], "buyer@example.com")


if __name__ == "__main__":
    unittest.main()
