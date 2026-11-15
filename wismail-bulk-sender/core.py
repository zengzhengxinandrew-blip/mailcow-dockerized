"""Token-free campaign import, scheduling, SMTP submission and optional IMAP feedback.

This module deliberately contains no AI client or model dependency.
"""

import csv
import base64
import datetime as dt
import email
import email.policy
import io
import imaplib
import hashlib
import hmac
import html
import json
import os
import re
import smtplib
import sqlite3
import ssl
import threading
import uuid
import zipfile
from email.message import EmailMessage
from xml.etree import ElementTree as ET

from cryptography.fernet import Fernet


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ROWS = 1000
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HEADERS = {
    "收件邮箱": "recipient_email", "recipient_email": "recipient_email", "to": "recipient_email",
    "邮件主题": "subject", "主题": "subject", "subject": "subject",
    "邮件正文": "body", "正文": "body", "body": "body",
}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value):
    return value.isoformat() if value else None


def parse_iso(value):
    return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL DEFAULT '__legacy_admin__',
          name TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'draft',
          created_at TEXT NOT NULL,
          start_at TEXT,
          interval_seconds INTEGER
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
          row_number INTEGER NOT NULL,
          recipient_email TEXT NOT NULL,
          subject TEXT NOT NULL,
          body TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          scheduled_at TEXT,
          next_attempt_at TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_attempt_at TEXT,
          submitted_at TEXT,
          replied_at TEXT,
          bounced_at TEXT,
          message_id TEXT UNIQUE,
          last_error TEXT,
          UNIQUE(batch_id, row_number)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_queue ON messages(status, next_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_email);
        CREATE TABLE IF NOT EXISTS imap_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS suppressions (
          owner_email TEXT NOT NULL DEFAULT '*',
          recipient_email TEXT NOT NULL,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(owner_email,recipient_email)
        );
        CREATE TABLE IF NOT EXISTS mailbox_auth (
          owner_email TEXT PRIMARY KEY,
          smtp_password_enc TEXT NOT NULL,
          imap_password_enc TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS imap_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          source_uid TEXT NOT NULL UNIQUE,
          matched_message_id INTEGER,
          observed_at TEXT NOT NULL,
          detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contacts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL,
          email TEXT NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          gender TEXT NOT NULL DEFAULT '',
          birthday TEXT NOT NULL DEFAULT '',
          phone TEXT NOT NULL DEFAULT '',
          region TEXT NOT NULL DEFAULT '',
          custom_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'subscribed',
          status_reason TEXT NOT NULL DEFAULT '',
          deleted_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(owner_email,email)
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_owner_status ON contacts(owner_email,status,deleted_at);
        CREATE TABLE IF NOT EXISTS contact_categories (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL,
          name TEXT NOT NULL,
          UNIQUE(owner_email,name)
        );
        CREATE TABLE IF NOT EXISTS contact_category_links (
          contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
          category_id INTEGER NOT NULL REFERENCES contact_categories(id) ON DELETE CASCADE,
          PRIMARY KEY(contact_id,category_id)
        );
        CREATE TABLE IF NOT EXISTS templates (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL,
          name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS template_versions (
          template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
          version INTEGER NOT NULL,
          subject TEXT NOT NULL,
          body_text TEXT NOT NULL,
          body_html TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          PRIMARY KEY(template_id,version)
        );
        CREATE TABLE IF NOT EXISTS template_test_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          template_id INTEGER NOT NULL REFERENCES templates(id),
          owner_email TEXT NOT NULL,
          recipient_email TEXT NOT NULL,
          status TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        batch_columns = [item["name"] for item in db.execute("PRAGMA table_info(batches)")]
        if "owner_email" not in batch_columns:
            db.execute("ALTER TABLE batches ADD COLUMN owner_email TEXT NOT NULL DEFAULT '__legacy_admin__'")
        message_columns = [item["name"] for item in db.execute("PRAGMA table_info(messages)")]
        if "body_html" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN body_html TEXT NOT NULL DEFAULT ''")
        suppression_columns = [item["name"] for item in db.execute("PRAGMA table_info(suppressions)")]
        if "owner_email" not in suppression_columns:
            db.execute("ALTER TABLE suppressions RENAME TO suppressions_legacy")
            db.execute("""CREATE TABLE suppressions (owner_email TEXT NOT NULL, recipient_email TEXT NOT NULL,
                          reason TEXT NOT NULL, created_at TEXT NOT NULL,
                          PRIMARY KEY(owner_email,recipient_email))""")
            db.execute("""INSERT INTO suppressions(owner_email,recipient_email,reason,created_at)
                          SELECT '*',recipient_email,reason,created_at FROM suppressions_legacy""")
            db.execute("DROP TABLE suppressions_legacy")
        # A restart during SMTP submission has an uncertain result. Never resend automatically.
        db.execute("UPDATE messages SET status='needs_review', last_error='发送进程中断；请核对外部邮箱后手动处理' WHERE status='sending'")


def _column_index(cell_ref):
    letters = re.match(r"[A-Z]+", cell_ref or "")
    if not letters:
        return None
    value = 0
    for letter in letters.group(0):
        value = value * 26 + ord(letter) - 64
    return value - 1


def _xlsx_rows(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        if sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Excel 解压后过大")
        names = archive.namelist()
        worksheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not worksheets:
            raise ValueError("Excel 中没有工作表")
        strings = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(NS + "si"):
                strings.append("".join(node.text or "" for node in item.iter(NS + "t")))
        root = ET.fromstring(archive.read(worksheets[0]))
        rows = []
        sheet_data = root.find(NS + "sheetData")
        if sheet_data is None:
            raise ValueError("Excel 工作表为空")
        for row in sheet_data.findall(NS + "row"):
            cells = {}
            for cell in row.findall(NS + "c"):
                index = _column_index(cell.get("r"))
                if index is None or index > 100:
                    continue
                if cell.find(NS + "f") is not None:
                    raise ValueError("请将公式转换为实际文本后上传")
                kind = cell.get("t")
                if kind == "inlineStr":
                    inner = cell.find(NS + "is")
                    value = "".join(node.text or "" for node in inner.iter(NS + "t")) if inner is not None else ""
                else:
                    raw = cell.findtext(NS + "v") or ""
                    value = strings[int(raw)] if kind == "s" and raw else raw
                cells[index] = value
            if cells:
                rows.append((int(row.get("r") or len(rows) + 1), cells))
            if len(rows) > MAX_ROWS + 1:
                raise ValueError("每批最多上传 {} 封邮件".format(MAX_ROWS))
        return rows
    except (zipfile.BadZipFile, ET.ParseError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("无法读取 Excel 文件：{}".format(exc))


def parse_upload(filename, data):
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("文件为空或超过 5 MB")
    filename = (filename or "").lower()
    if filename.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("CSV 请使用 UTF-8 编码")
        reader = csv.reader(io.StringIO(text, newline=""))
        raw_rows = [(i, {j: value for j, value in enumerate(row)}) for i, row in enumerate(reader, 1) if any(x.strip() for x in row)]
    elif filename.endswith(".xlsx"):
        raw_rows = _xlsx_rows(data)
    else:
        raise ValueError("仅支持 .xlsx 或 UTF-8 .csv")
    if len(raw_rows) < 2:
        raise ValueError("文件需要表头和至少一封邮件")
    header = {HEADERS.get(str(v).strip().lower()): k for k, v in raw_rows[0][1].items() if HEADERS.get(str(v).strip().lower())}
    if set(header) != {"recipient_email", "subject", "body"}:
        raise ValueError("表头必须包含：收件邮箱、邮件主题、邮件正文")
    messages, errors, seen = [], [], set()
    for row_number, cells in raw_rows[1:]:
        recipient = str(cells.get(header["recipient_email"], "")).strip()
        subject = str(cells.get(header["subject"], "")).strip()
        body = str(cells.get(header["body"], "")).strip()
        issues = []
        if not EMAIL_RE.match(recipient) or len(recipient) > 254:
            issues.append("收件邮箱无效")
        if not subject or len(subject) > 998:
            issues.append("邮件主题为空或过长")
        if not body or len(body) > 100000:
            issues.append("邮件正文为空或过长")
        if re.search(r"\{\{[^}]+\}\}|\{(?:客户名|产品名|邮箱)\}", subject + body):
            issues.append("存在未填完的占位符")
        fingerprint = (recipient.lower(), subject, body)
        if fingerprint in seen:
            issues.append("与同批其他邮件完全重复")
        seen.add(fingerprint)
        if issues:
            errors.append("第 {} 行：{}".format(row_number, "、".join(issues)))
        else:
            messages.append((row_number, recipient, subject, body))
    if len(messages) > MAX_ROWS:
        errors.append("每批最多上传 {} 封邮件".format(MAX_ROWS))
    if errors:
        raise ValueError("上传检查未通过：\n" + "\n".join(errors[:30]) + ("\n还有更多错误" if len(errors) > 30 else ""))
    return messages


def create_draft(path, name, messages, owner_email="__legacy_admin__"):
    with connect(path) as db:
        blocked = []
        for message in messages:
            row_number, recipient = message[:2]
            if db.execute("SELECT 1 FROM suppressions WHERE recipient_email=? AND owner_email IN (?, '*')",
                          (recipient.lower(), owner_email)).fetchone():
                blocked.append("第 {} 行：{} 已在不再联系名单".format(row_number, recipient))
            elif contact_blocked(db, owner_email, recipient):
                blocked.append("第 {} 行：{} 的联系人状态禁止发送".format(row_number, recipient))
        if blocked:
            raise ValueError("上传检查未通过：\n" + "\n".join(blocked[:30]))
        cur = db.execute("INSERT INTO batches(owner_email,name,state,created_at) VALUES (?,?,?,?)",
                         (owner_email, name[:120] or "未命名批次", "draft", iso(utcnow())))
        batch_id = cur.lastrowid
        db.executemany("INSERT INTO messages(batch_id,row_number,recipient_email,subject,body,body_html) VALUES (?,?,?,?,?,?)",
                       [(batch_id,) + tuple(row[:4]) + (row[4] if len(row) > 4 else "",) for row in messages])
    return batch_id


VARIABLE_RE = re.compile(r"\{\{([^{}]+)\}\}")


def save_template(path, owner_email, name, subject, body_text, body_html="", template_id=None):
    name, subject, body_text, body_html = [str(value or "").strip() for value in (name, subject, body_text, body_html)]
    if not name or len(name) > 120 or not subject or len(subject) > 998 or "\n" in subject:
        raise ValueError("模板名称或主题无效")
    if not body_text or len(body_text) > 100000 or len(body_html) > 200000:
        raise ValueError("请填写纯文本正文，且不要超过长度限制")
    now = iso(utcnow())
    with connect(path) as db:
        if template_id:
            row = db.execute("SELECT id FROM templates WHERE id=? AND owner_email=?", (template_id, owner_email)).fetchone()
            if not row:
                raise ValueError("模板不存在")
            db.execute("UPDATE templates SET name=?,updated_at=? WHERE id=?", (name, now, template_id))
        else:
            template_id = db.execute("INSERT INTO templates(owner_email,name,created_at,updated_at) VALUES (?,?,?,?)",
                                     (owner_email, name, now, now)).lastrowid
        version = db.execute("SELECT COALESCE(MAX(version),0)+1 AS n FROM template_versions WHERE template_id=?",
                             (template_id,)).fetchone()["n"]
        db.execute("INSERT INTO template_versions(template_id,version,subject,body_text,body_html,created_at) VALUES (?,?,?,?,?,?)",
                   (template_id, version, subject, body_text, body_html, now))
    return template_id, version


def get_template(path, owner_email, template_id, version=None):
    with connect(path) as db:
        row = db.execute("""SELECT t.id,t.name,v.version,v.subject,v.body_text,v.body_html,v.created_at
                            FROM templates t JOIN template_versions v ON v.template_id=t.id
                            WHERE t.id=? AND t.owner_email=? AND v.version=COALESCE(?,
                            (SELECT MAX(version) FROM template_versions WHERE template_id=t.id))""",
                         (template_id, owner_email, version)).fetchone()
    if not row:
        raise ValueError("模板不存在")
    return dict(row)


def render_template(template, contact):
    fields = {"邮箱": contact["email"], "姓名": contact["name"], "性别": contact["gender"],
              "生日": contact["birthday"], "手机": contact["phone"], "地区": contact["region"]}
    fields.update({"变量" + str(i): json.loads(contact["custom_json"]).get("custom_" + str(i), "")
                   for i in range(1, 11)})
    def replace(match, escape=False):
        key = match.group(1).strip()
        if key not in fields or not fields[key]:
            raise ValueError("模板变量未填写：" + key)
        value = str(fields[key])
        return html.escape(value, quote=True) if escape else value
    subject = VARIABLE_RE.sub(lambda match: replace(match), template["subject"])
    if "\r" in subject or "\n" in subject or len(subject) > 998:
        raise ValueError("展开后的邮件主题无效")
    text = VARIABLE_RE.sub(lambda match: replace(match), template["body_text"])
    body_html = VARIABLE_RE.sub(lambda match: replace(match, True), template["body_html"])
    return subject, text, body_html


def create_contact_campaign(path, owner_email, name, template_id, category=""):
    template = get_template(path, owner_email, template_id)
    query = """SELECT c.* FROM contacts c WHERE c.owner_email=? AND c.status='subscribed' AND c.deleted_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.recipient_email=c.email AND s.owner_email IN (c.owner_email,'*'))"""
    params = [owner_email]
    if category:
        query += " AND EXISTS (SELECT 1 FROM contact_category_links l JOIN contact_categories g ON g.id=l.category_id WHERE l.contact_id=c.id AND g.owner_email=? AND g.name=?)"
        params += [owner_email, category]
    with connect(path) as db:
        contacts = db.execute(query + " ORDER BY c.id LIMIT ?", params + [MAX_ROWS + 1]).fetchall()
    if not contacts or len(contacts) > MAX_ROWS:
        raise ValueError("筛选结果为空或超过每批 1000 人")
    messages = []
    for number, contact in enumerate(contacts, 1):
        try:
            subject, text, body_html = render_template(template, contact)
        except ValueError as exc:
            raise ValueError("{}：{}".format(contact["email"], exc))
        messages.append((number, contact["email"], subject, text, body_html))
    return create_draft(path, name, messages, owner_email)


def send_template_test(path, owner_email, template_id, contact_id, recipient, settings):
    account = mailbox_settings(path, owner_email, settings)
    if not account or not all(account.get(key) for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")):
        raise ValueError("请先配置发件授权")
    recipient = recipient.strip().lower()
    if recipient != account["SMTP_FROM"].lower():
        raise ValueError("测试邮件只能发送给当前发件邮箱")
    with connect(path) as db:
        contact = db.execute("SELECT * FROM contacts WHERE id=? AND owner_email=?", (contact_id, owner_email)).fetchone()
    if not contact:
        raise ValueError("请选择用于变量预览的联系人")
    subject, text, body_html = render_template(get_template(path, owner_email, template_id), contact)
    row = {"owner_email": owner_email, "recipient_email": recipient, "subject": "[测试] " + subject,
           "body": text, "body_html": body_html}
    try:
        detail = _smtp_submit(row, account)
        status = "submitted"
    except Exception as exc:
        status = "failed"
        detail = str(exc).replace(account["SMTP_PASSWORD"], "[redacted]")[:300]
    with connect(path) as db:
        db.execute("INSERT INTO template_test_logs(template_id,owner_email,recipient_email,status,detail,created_at) VALUES (?,?,?,?,?,?)",
                   (template_id, owner_email, recipient, status, detail, iso(utcnow())))
    if status == "failed":
        raise ValueError("测试邮件提交失败，请查看测试记录")
    return detail


def add_suppression(path, recipient, reason="手动添加", owner_email="__legacy_admin__"):
    recipient = recipient.strip().lower()
    if not EMAIL_RE.match(recipient):
        raise ValueError("不再联系邮箱格式无效")
    with connect(path) as db:
        db.execute("INSERT OR REPLACE INTO suppressions(owner_email,recipient_email,reason,created_at) VALUES (?,?,?,?)",
                   (owner_email, recipient, reason[:200], iso(utcnow())))
        db.execute("""UPDATE contacts SET status='unsubscribed',status_reason=?,updated_at=?
                      WHERE email=? AND status='subscribed' AND (?='*' OR owner_email=?)""",
                   (reason[:200], iso(utcnow()), recipient, owner_email, owner_email))
        db.execute("""UPDATE messages SET status='suppressed',last_error='已加入不再联系名单'
                      WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                      (SELECT id FROM batches WHERE owner_email=?)""", (recipient, owner_email))


CONTACT_STATUSES = {"subscribed", "unsubscribed", "complained", "invalid"}
CONTACT_COLUMNS = {"邮箱": "email", "email": "email", "姓名": "name", "name": "name",
                   "性别": "gender", "gender": "gender", "生日": "birthday", "birthday": "birthday",
                   "手机": "phone", "phone": "phone", "地区": "region", "region": "region",
                   "分类": "categories", "categories": "categories"}


def parse_contacts_upload(filename, data):
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("联系人文件为空或超过 5 MB")
    if filename.lower().endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("联系人 CSV 请使用 UTF-8 编码")
        rows = list(csv.reader(io.StringIO(text, newline="")))
        raw = [(number, dict(enumerate(row))) for number, row in enumerate(rows, 1) if any(v.strip() for v in row)]
    elif filename.lower().endswith(".xlsx"):
        raw = _xlsx_rows(data)
    else:
        raise ValueError("联系人文件仅支持 .xlsx 或 UTF-8 .csv")
    if len(raw) < 2 or len(raw) > 1001:
        raise ValueError("联系人文件需有表头，且每次最多 1000 人")
    columns = {}
    for index, value in raw[0][1].items():
        label = str(value).strip().lower()
        key = CONTACT_COLUMNS.get(label)
        match = re.fullmatch(r"(?:变量|custom_)(10|[1-9])", label)
        if match:
            key = "custom_" + match.group(1)
        if key:
            if key in columns:
                raise ValueError("联系人表头重复：" + key)
            columns[key] = index
    if "email" not in columns:
        raise ValueError("联系人表头缺少邮箱列")
    contacts, errors = [], []
    for number, cells in raw[1:]:
        item = {key: str(cells.get(index, "")).strip() for key, index in columns.items()}
        address = item["email"].lower()
        if not EMAIL_RE.fullmatch(address) or len(address) > 254:
            errors.append("第 {} 行邮箱无效".format(number))
            continue
        item["email"] = address
        contacts.append(item)
    if errors:
        raise ValueError("；".join(errors[:20]))
    return contacts


def save_contact(path, owner_email, item):
    address = str(item.get("email", "")).strip().lower()
    if not EMAIL_RE.fullmatch(address) or len(address) > 254:
        raise ValueError("联系人邮箱无效")
    fields = {key: str(item.get(key, "")).strip()[:200] for key in ("name", "gender", "birthday", "phone", "region")}
    custom = {"custom_" + str(i): str(item.get("custom_" + str(i), "")).strip()[:500]
              for i in range(1, 11) if item.get("custom_" + str(i))}
    categories = [name.strip()[:80] for name in re.split(r"[,，;；]", str(item.get("categories", ""))) if name.strip()]
    now = iso(utcnow())
    with connect(path) as db:
        existing = db.execute("SELECT id FROM contacts WHERE owner_email=? AND email=?", (owner_email, address)).fetchone()
        values = (fields["name"], fields["gender"], fields["birthday"], fields["phone"], fields["region"],
                  json.dumps(custom, ensure_ascii=False))
        if existing:
            contact_id = existing["id"]
            db.execute("""UPDATE contacts SET name=?,gender=?,birthday=?,phone=?,region=?,custom_json=?,updated_at=?
                          WHERE id=?""", values + (now, contact_id))
        else:
            contact_id = db.execute("""INSERT INTO contacts(owner_email,email,name,gender,birthday,phone,region,custom_json,created_at,updated_at)
                                      VALUES (?,?,?,?,?,?,?,?,?,?)""", (owner_email, address) + values + (now, now)).lastrowid
        suppression = db.execute("SELECT reason FROM suppressions WHERE recipient_email=? AND owner_email IN (?, '*') LIMIT 1",
                                 (address, owner_email)).fetchone()
        if suppression:
            db.execute("""UPDATE contacts SET status='unsubscribed',status_reason=?
                          WHERE id=? AND status='subscribed'""", (suppression["reason"], contact_id))
        for name in dict.fromkeys(categories):
            db.execute("INSERT OR IGNORE INTO contact_categories(owner_email,name) VALUES (?,?)", (owner_email, name))
            category_id = db.execute("SELECT id FROM contact_categories WHERE owner_email=? AND name=?", (owner_email, name)).fetchone()["id"]
            db.execute("INSERT OR IGNORE INTO contact_category_links(contact_id,category_id) VALUES (?,?)", (contact_id, category_id))
    return contact_id


def import_contacts(path, owner_email, contacts):
    for item in contacts:
        save_contact(path, owner_email, item)
    return len(contacts)


def set_contact_status(path, owner_email, contact_id, status, reason=""):
    if status not in CONTACT_STATUSES - {"subscribed"}:
        raise ValueError("不支持的联系人状态")
    with connect(path) as db:
        row = db.execute("SELECT email FROM contacts WHERE id=? AND owner_email=?", (contact_id, owner_email)).fetchone()
        if not row:
            raise ValueError("联系人不存在")
        db.execute("UPDATE contacts SET status=?,status_reason=?,updated_at=? WHERE id=?",
                   (status, reason[:200], iso(utcnow()), contact_id))
        db.execute("""UPDATE messages SET status='suppressed',last_error='联系人已停止发送'
                      WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                      (SELECT id FROM batches WHERE owner_email=?)""", (row["email"], owner_email))


def set_contact_deleted(path, owner_email, contact_id, deleted):
    with connect(path) as db:
        row = db.execute("SELECT email FROM contacts WHERE id=? AND owner_email=?", (contact_id, owner_email)).fetchone()
        if not row:
            raise ValueError("联系人不存在")
        changed = db.execute("UPDATE contacts SET deleted_at=?,updated_at=? WHERE id=? AND owner_email=?",
                             (iso(utcnow()) if deleted else None, iso(utcnow()), contact_id, owner_email))
        if deleted:
            db.execute("""UPDATE messages SET status='suppressed',last_error='联系人已移入回收站'
                          WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                          (SELECT id FROM batches WHERE owner_email=?)""", (row["email"], owner_email))


def contact_blocked(db, owner_email, address):
    row = db.execute("SELECT status,deleted_at FROM contacts WHERE owner_email=? AND email=?",
                     (owner_email, address.lower())).fetchone()
    return bool(row and (row["status"] != "subscribed" or row["deleted_at"]))


def unsubscribe_token(owner_email, recipient_email, secret):
    if not secret:
        raise ValueError("退订签名密钥未配置")
    payload = (owner_email + "\0" + recipient_email.lower()).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_unsubscribe_token(token, secret):
    if not secret or len(token) > 1200 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", token):
        raise ValueError("退订链接无效")
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("退订链接无效")
    try:
        owner, recipient = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8").split("\0", 1)
    except (ValueError, UnicodeError):
        raise ValueError("退订链接无效")
    if not owner or not EMAIL_RE.fullmatch(recipient):
        raise ValueError("退订链接无效")
    return owner, recipient


def unsubscribe_url(settings, owner_email, recipient_email):
    base = settings.get("OAUTH_BASE_URL", "").rstrip("/")
    secret = settings.get("SESSION_SECRET", "")
    if not base or not secret:
        return ""
    from urllib.parse import quote
    return base + settings.get("URL_PREFIX", "").rstrip("/") + "/unsubscribe?token=" + quote(
        unsubscribe_token(owner_email, recipient_email, secret))


def save_mailbox_auth(path, owner_email, smtp_password, imap_password, encryption_key):
    if not smtp_password:
        raise ValueError("请输入 SMTP 应用密码")
    cipher = Fernet(encryption_key.encode("ascii"))
    smtp_enc = cipher.encrypt(smtp_password.encode("utf-8")).decode("ascii")
    imap_enc = cipher.encrypt(imap_password.encode("utf-8")).decode("ascii") if imap_password else None
    with connect(path) as db:
        db.execute("INSERT OR REPLACE INTO mailbox_auth(owner_email,smtp_password_enc,imap_password_enc,updated_at) VALUES (?,?,?,?)",
                   (owner_email, smtp_enc, imap_enc, iso(utcnow())))


def revoke_mailbox_auth(path, owner_email):
    with connect(path) as db:
        db.execute("DELETE FROM mailbox_auth WHERE owner_email=?", (owner_email,))
        db.execute("UPDATE batches SET state='paused' WHERE owner_email=? AND state='active'", (owner_email,))


def mailbox_settings(path, owner_email, settings):
    if owner_email == "__legacy_admin__":
        return settings
    with connect(path) as db:
        row = db.execute("SELECT * FROM mailbox_auth WHERE owner_email=?", (owner_email,)).fetchone()
    if not row or not settings.get("CREDENTIAL_KEY"):
        return None
    cipher = Fernet(settings["CREDENTIAL_KEY"].encode("ascii"))
    result = dict(settings)
    result["SMTP_USER"] = owner_email
    result["SMTP_FROM"] = owner_email
    result["SMTP_PASSWORD"] = cipher.decrypt(row["smtp_password_enc"].encode("ascii")).decode("utf-8")
    result["IMAP_USER"] = owner_email
    result["IMAP_PASSWORD"] = (cipher.decrypt(row["imap_password_enc"].encode("ascii")).decode("utf-8")
                               if row["imap_password_enc"] else "")
    return result


def verify_mailbox_auth(owner_email, smtp_password, imap_password, settings):
    """Verify mailbox credentials without sending any email."""
    if not settings.get("SMTP_HOST"):
        raise ValueError("服务器尚未配置 SMTP 主机")
    port = int(settings.get("SMTP_PORT", 587))
    if port == 465:
        smtp = smtplib.SMTP_SSL(settings["SMTP_HOST"], port, timeout=15, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(settings["SMTP_HOST"], port, timeout=15)
    try:
        if port != 465:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(owner_email, smtp_password)
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()
    if imap_password:
        if not settings.get("IMAP_HOST"):
            raise ValueError("服务器尚未配置 IMAP 主机")
        mailbox = imaplib.IMAP4_SSL(settings["IMAP_HOST"], int(settings.get("IMAP_PORT", 993)))
        try:
            mailbox.login(owner_email, imap_password)
        finally:
            try:
                mailbox.logout()
            except Exception:
                pass


def schedule_batch(path, batch_id, start_local, interval_seconds, offset_hours=8):
    if not 60 <= interval_seconds <= 86400:
        raise ValueError("发送间隔需为 60 秒至 24 小时")
    if start_local == "now":
        start = utcnow()
    else:
        try:
            local = dt.datetime.strptime(start_local, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise ValueError("发送时间格式无效")
        start = (local - dt.timedelta(hours=offset_hours)).replace(tzinfo=dt.timezone.utc)
        if start < utcnow() - dt.timedelta(minutes=1):
            raise ValueError("开始时间不能早于当前时间")
    with connect(path) as db:
        batch = db.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["state"] != "draft":
            raise ValueError("这个批次已启动或不存在")
        rows = db.execute("SELECT id FROM messages WHERE batch_id=? ORDER BY row_number", (batch_id,)).fetchall()
        for index, row in enumerate(rows):
            due = iso(start + dt.timedelta(seconds=index * interval_seconds))
            db.execute("UPDATE messages SET status='queued',scheduled_at=?,next_attempt_at=? WHERE id=?", (due, due, row["id"]))
        db.execute("UPDATE batches SET state='active',start_at=?,interval_seconds=? WHERE id=?",
                   (iso(start), interval_seconds, batch_id))


def set_batch_state(path, batch_id, state):
    if state not in ("paused", "active"):
        raise ValueError("无效状态")
    expected = "active" if state == "paused" else "paused"
    with connect(path) as db:
        changed = db.execute("UPDATE batches SET state=? WHERE id=? AND state=?", (state, batch_id, expected)).rowcount
        if not changed:
            raise ValueError("无法切换这个批次的状态")


def cancel_batch(path, batch_id):
    with connect(path) as db:
        db.execute("UPDATE batches SET state='cancelled' WHERE id=? AND state IN ('active','paused')", (batch_id,))
        db.execute("UPDATE messages SET status='cancelled' WHERE batch_id=? AND status='queued'", (batch_id,))


def _smtp_submit(row, settings):
    sender = settings["SMTP_FROM"]
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = row["recipient_email"]
    msg["Subject"] = row["subject"]
    msg["Date"] = email.utils.format_datetime(utcnow())
    message_id = "<{}@{}>".format(uuid.uuid4().hex, sender.rsplit("@", 1)[1])
    msg["Message-ID"] = message_id
    link = unsubscribe_url(settings, row["owner_email"], row["recipient_email"]) if "owner_email" in row.keys() else ""
    msg.set_content(row["body"] + ("\n\n不再接收此类邮件：" + link if link else ""))
    if "body_html" in row.keys() and row["body_html"]:
        footer = ('<p>不再接收此类邮件：<a href="{0}">退订</a></p>'.format(html.escape(link, quote=True)) if link else '')
        msg.add_alternative(row["body_html"] + footer, subtype="html")
    if link:
        msg["List-Unsubscribe"] = "<{}>".format(link)
    port = int(settings["SMTP_PORT"])
    timeout = 30
    if port == 465:
        smtp = smtplib.SMTP_SSL(settings["SMTP_HOST"], port, timeout=timeout, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(settings["SMTP_HOST"], port, timeout=timeout)
    try:
        if port != 465:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(settings["SMTP_USER"], settings["SMTP_PASSWORD"])
        refused = smtp.send_message(msg, from_addr=sender, to_addrs=[row["recipient_email"]])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()
    return message_id


def dispatch_one(path, settings):
    """Attempt at most one due message. Returns True only if one was claimed."""
    if not settings.get("SMTP_HOST") or not settings.get("SMTP_PORT"):
        return False
    now = utcnow()
    with connect(path) as db:
        db.execute("""UPDATE messages SET status='suppressed',last_error='已加入不再联系名单'
                      WHERE id IN (SELECT m.id FROM messages m JOIN batches b ON b.id=m.batch_id
                      JOIN suppressions s ON s.recipient_email=lower(m.recipient_email)
                      AND s.owner_email IN (b.owner_email,'*') WHERE m.status='queued')""")
        db.execute("""UPDATE messages SET status='suppressed',last_error='联系人状态禁止发送'
                      WHERE status='queued' AND EXISTS (
                        SELECT 1 FROM batches b JOIN contacts c ON c.owner_email=b.owner_email
                        WHERE b.id=messages.batch_id AND c.email=lower(messages.recipient_email)
                        AND (c.status!='subscribed' OR c.deleted_at IS NOT NULL))""")
        db.execute("""UPDATE batches SET state='completed' WHERE state='active'
                      AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.batch_id=batches.id AND m.status IN ('queued','sending'))""")
        offset = int(settings.get("UTC_OFFSET_HOURS", 8))
        local_day = (now + dt.timedelta(hours=offset)).date()
        day_start = dt.datetime.combine(local_day, dt.time()).replace(tzinfo=dt.timezone.utc) - dt.timedelta(hours=offset)
        candidates = db.execute("""SELECT m.*,b.owner_email FROM messages m JOIN batches b ON b.id=m.batch_id
                            WHERE m.status='queued' AND b.state='active' AND m.next_attempt_at<=?
                            ORDER BY m.next_attempt_at,m.id LIMIT 100""", (iso(now),)).fetchall()
        row, sender_settings = None, None
        for candidate in candidates:
            owner = candidate["owner_email"]
            account = mailbox_settings(path, owner, settings)
            if not account or not all(account.get(key) for key in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")):
                continue
            last = db.execute("""SELECT MAX(m.last_attempt_at) AS value FROM messages m
                                 JOIN batches b ON b.id=m.batch_id WHERE b.owner_email=?""", (owner,)).fetchone()["value"]
            if last and parse_iso(last) + dt.timedelta(seconds=int(settings.get("MIN_INTERVAL_SECONDS", 60))) > now:
                continue
            sent_today = db.execute("""SELECT COUNT(*) AS n FROM messages m JOIN batches b ON b.id=m.batch_id
                                       WHERE b.owner_email=? AND m.submitted_at>=?""", (owner, iso(day_start))).fetchone()["n"]
            if sent_today >= int(settings.get("DAILY_LIMIT", 100)):
                continue
            hour_start = iso(now - dt.timedelta(hours=1))
            sent_hour = db.execute("""SELECT COUNT(*) AS n FROM messages m JOIN batches b ON b.id=m.batch_id
                                      WHERE b.owner_email=? AND m.submitted_at>=?""", (owner, hour_start)).fetchone()["n"]
            if sent_hour >= int(settings.get("HOURLY_LIMIT", 20)):
                continue
            row, sender_settings = candidate, account
            break
        if not row:
            return False
        db.execute("UPDATE messages SET status='sending', attempt_count=attempt_count+1, last_attempt_at=? WHERE id=? AND status='queued'",
                   (iso(now), row["id"]))
    try:
        message_id = _smtp_submit(row, sender_settings)
    except Exception as exc:
        code = getattr(exc, "smtp_code", None)
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            response = list(exc.recipients.values())[0]
            code = response[0] if response else None
        temporary = code is None or 400 <= int(code) < 500
        retry = temporary and row["attempt_count"] + 1 < 3
        next_at = iso(now + dt.timedelta(minutes=10 * (row["attempt_count"] + 1))) if retry else None
        error = str(exc).replace(sender_settings.get("SMTP_PASSWORD", "\0"), "[redacted]")[:500]
        with connect(path) as db:
            db.execute("UPDATE messages SET status=?,next_attempt_at=?,last_error=? WHERE id=?",
                       ("queued" if retry else "failed", next_at, error, row["id"]))
    else:
        with connect(path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=?,message_id=?,last_error=NULL WHERE id=?",
                       (iso(now), message_id, row["id"]))
    return True


def _extract_dsn_recipient(message):
    for part in message.walk():
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            for block in payload if isinstance(payload, list) else []:
                raw = block.get("Final-Recipient") or block.get("Original-Recipient")
                if raw:
                    return raw.split(";", 1)[-1].strip().lower()
    raw = message.get("X-Failed-Recipients")
    return raw.strip().lower() if raw else None


def poll_imap(path, settings, owner_email="__legacy_admin__"):
    """Best-effort reply/bounce detection from the configured mailbox INBOX."""
    required = ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD")
    if not all(settings.get(key) for key in required):
        return 0
    mailbox = imaplib.IMAP4_SSL(settings["IMAP_HOST"], int(settings.get("IMAP_PORT", 993)))
    try:
        mailbox.login(settings["IMAP_USER"], settings["IMAP_PASSWORD"])
        mailbox.select("INBOX", readonly=True)
        state_key = "last_uid:" + owner_email
        with connect(path) as db:
            row = db.execute("SELECT value FROM imap_state WHERE key=?", (state_key,)).fetchone()
            last_uid = int(row["value"]) if row else 0
        status, data = mailbox.uid("SEARCH", None, "UID", "{}:*".format(last_uid + 1))
        if status != "OK":
            return 0
        uids = [int(x) for x in (data[0] or b"").split() if int(x) > last_uid]
        processed = 0
        for uid in uids[:200]:
            status, parts = mailbox.uid("FETCH", str(uid), "(RFC822)")
            if status != "OK":
                continue
            raw = next((item[1] for item in parts if isinstance(item, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            references = " ".join([str(msg.get("In-Reply-To", "")), str(msg.get("References", ""))])
            ids = MESSAGE_ID_RE.findall(references)
            recipient = _extract_dsn_recipient(msg)
            kind = "bounce" if recipient else "reply"
            matched = None
            with connect(path) as db:
                for message_id in ids:
                    matched = db.execute("""SELECT m.id FROM messages m JOIN batches b ON b.id=m.batch_id
                                            WHERE m.message_id=? AND b.owner_email=?""", (message_id, owner_email)).fetchone()
                    if matched:
                        break
                if not matched and recipient:
                    candidates = db.execute("""SELECT m.id FROM messages m JOIN batches b ON b.id=m.batch_id
                                               WHERE b.owner_email=? AND lower(m.recipient_email)=? AND m.submitted_at>=?
                                               ORDER BY m.submitted_at DESC LIMIT 2""",
                                            (owner_email, recipient, iso(utcnow() - dt.timedelta(days=14)))).fetchall()
                    if len(candidates) == 1:
                        matched = candidates[0]
                matched_id = matched["id"] if matched else None
                if matched_id:
                    field = "bounced_at" if kind == "bounce" else "replied_at"
                    db.execute("UPDATE messages SET {}=COALESCE({},?) WHERE id=?".format(field, field),
                               (iso(utcnow()), matched_id))
                db.execute("INSERT OR IGNORE INTO imap_events(kind,source_uid,matched_message_id,observed_at,detail) VALUES (?,?,?,?,?)",
                           (kind, owner_email + ":" + str(uid), matched_id, iso(utcnow()), recipient or str(msg.get("From", ""))[:200]))
                db.execute("INSERT OR REPLACE INTO imap_state(key,value) VALUES (?,?)", (state_key, str(uid)))
            processed += 1
        return processed
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


class Worker(threading.Thread):
    def __init__(self, path, settings, stop_event=None):
        super(Worker, self).__init__(daemon=True)
        self.path = path
        self.settings = settings
        self.stop_event = stop_event or threading.Event()

    def run(self):
        next_imap = utcnow()
        while not self.stop_event.is_set():
            try:
                dispatch_one(self.path, self.settings)
            except Exception as exc:
                print("发送任务检查失败：{}".format(exc))
            if utcnow() >= next_imap:
                with connect(self.path) as db:
                    owners = [row["owner_email"] for row in db.execute("SELECT owner_email FROM mailbox_auth")]
                if self.settings.get("IMAP_USER") and self.settings.get("IMAP_PASSWORD"):
                    owners.append("__legacy_admin__")
                for owner in owners:
                    try:
                        account = mailbox_settings(self.path, owner, self.settings)
                        if account:
                            poll_imap(self.path, account, owner)
                    except Exception as exc:
                        print("IMAP 状态同步失败：{}".format(exc))
                next_imap = utcnow() + dt.timedelta(seconds=120)
            self.stop_event.wait(5)
