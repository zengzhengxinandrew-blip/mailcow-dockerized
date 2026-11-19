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
import ipaddress
import hashlib
import hmac
import html
import json
import math
import os
import re
import smtplib
import sqlite3
import ssl
import threading
import uuid
import zipfile
from email.message import EmailMessage
from html.parser import HTMLParser
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from cryptography.fernet import Fernet


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ROWS = 1000
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
REPLY_ADDRESS_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
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
          interval_seconds INTEGER,
          hourly_limit INTEGER,
          reply_to TEXT NOT NULL DEFAULT '',
          ab_mode INTEGER NOT NULL DEFAULT 0,
          track_open INTEGER NOT NULL DEFAULT 0,
          track_click INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
          row_number INTEGER NOT NULL,
          recipient_email TEXT NOT NULL,
          sender_email TEXT NOT NULL DEFAULT '',
          subject TEXT NOT NULL,
          body TEXT NOT NULL,
          template_id INTEGER,
          template_name TEXT NOT NULL DEFAULT '',
          template_version INTEGER,
          variant TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft',
          scheduled_at TEXT,
          next_attempt_at TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_attempt_at TEXT,
          submitted_at TEXT,
          opened_at TEXT,
          clicked_at TEXT,
          replied_at TEXT,
          bounced_at TEXT,
          message_id TEXT UNIQUE,
          last_error TEXT,
          UNIQUE(batch_id, row_number)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_queue ON messages(status, next_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_email);
        CREATE INDEX IF NOT EXISTS idx_messages_submitted_at ON messages(submitted_at);
        CREATE INDEX IF NOT EXISTS idx_messages_last_attempt_at ON messages(last_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_messages_bounced_at ON messages(bounced_at);
        CREATE INDEX IF NOT EXISTS idx_messages_replied_at ON messages(replied_at);
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
        CREATE TABLE IF NOT EXISTS sender_aliases (
          owner_email TEXT NOT NULL,
          alias_email TEXT NOT NULL,
          verified_at TEXT NOT NULL,
          PRIMARY KEY(owner_email,alias_email)
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
          merged_into_contact_id INTEGER REFERENCES contacts(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(owner_email,email)
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_owner_status ON contacts(owner_email,status,deleted_at);
        CREATE TABLE IF NOT EXISTS audit_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL,
          action TEXT NOT NULL,
          target_id INTEGER,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_events_owner ON audit_events(owner_email,id);
        CREATE TABLE IF NOT EXISTS provider_feedback_imports (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL,
          source_name TEXT NOT NULL,
          file_sha256 TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(owner_email,file_sha256)
        );
        CREATE TABLE IF NOT EXISTS provider_feedback_events (
          import_id INTEGER NOT NULL REFERENCES provider_feedback_imports(id) ON DELETE CASCADE,
          row_number INTEGER NOT NULL,
          recipient_email TEXT NOT NULL,
          status TEXT NOT NULL,
          previous_status TEXT NOT NULL,
          PRIMARY KEY(import_id,row_number)
        );
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
        CREATE TABLE IF NOT EXISTS automation_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL,
          event_kind TEXT NOT NULL,
          category_name TEXT NOT NULL,
          followup_template_id INTEGER,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          UNIQUE(owner_email,event_kind,category_name)
        );
        CREATE TABLE IF NOT EXISTS automation_runs (
          rule_id INTEGER NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
          event_key TEXT NOT NULL,
          contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
          applied_at TEXT NOT NULL,
          PRIMARY KEY(rule_id,event_key)
        );
        CREATE INDEX IF NOT EXISTS idx_automation_runs_contact ON automation_runs(contact_id,applied_at);
        CREATE TABLE IF NOT EXISTS automation_drafts (
          rule_id INTEGER NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
          event_key TEXT NOT NULL,
          contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
          batch_id INTEGER REFERENCES batches(id) ON DELETE SET NULL,
          status TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          PRIMARY KEY(rule_id,event_key)
        );
        CREATE TABLE IF NOT EXISTS scheduled_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_email TEXT NOT NULL,
          kind TEXT NOT NULL,
          schedule_value TEXT NOT NULL,
          target_category TEXT NOT NULL,
          template_id INTEGER NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          UNIQUE(owner_email,kind,schedule_value,target_category,template_id)
        );
        CREATE TABLE IF NOT EXISTS scheduled_runs (
          rule_id INTEGER NOT NULL REFERENCES scheduled_rules(id) ON DELETE CASCADE,
          occurrence_date TEXT NOT NULL,
          contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
          batch_id INTEGER REFERENCES batches(id) ON DELETE SET NULL,
          status TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          PRIMARY KEY(rule_id,occurrence_date,contact_id)
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
        CREATE TABLE IF NOT EXISTS delivery_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          smtp_code INTEGER,
          observed_at TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_events_message ON delivery_events(message_id,id);
        CREATE TABLE IF NOT EXISTS notification_preferences (
          owner_email TEXT PRIMARY KEY,
          failures_enabled INTEGER NOT NULL DEFAULT 1,
          bounces_enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS notification_reads (
          owner_email TEXT NOT NULL,
          event_id INTEGER NOT NULL REFERENCES delivery_events(id) ON DELETE CASCADE,
          read_at TEXT NOT NULL,
          PRIMARY KEY(owner_email,event_id)
        );
        """)
        batch_columns = [item["name"] for item in db.execute("PRAGMA table_info(batches)")]
        if "owner_email" not in batch_columns:
            db.execute("ALTER TABLE batches ADD COLUMN owner_email TEXT NOT NULL DEFAULT '__legacy_admin__'")
        if "reply_to" not in batch_columns:
            db.execute("ALTER TABLE batches ADD COLUMN reply_to TEXT NOT NULL DEFAULT ''")
        if "hourly_limit" not in batch_columns:
            db.execute("ALTER TABLE batches ADD COLUMN hourly_limit INTEGER")
        if "ab_mode" not in batch_columns:
            db.execute("ALTER TABLE batches ADD COLUMN ab_mode INTEGER NOT NULL DEFAULT 0")
        rule_columns = [item["name"] for item in db.execute("PRAGMA table_info(automation_rules)")]
        if "followup_template_id" not in rule_columns:
            db.execute("ALTER TABLE automation_rules ADD COLUMN followup_template_id INTEGER")
        for column in ("track_open", "track_click"):
            if column not in batch_columns:
                db.execute("ALTER TABLE batches ADD COLUMN {} INTEGER NOT NULL DEFAULT 0".format(column))
        message_columns = [item["name"] for item in db.execute("PRAGMA table_info(messages)")]
        if "body_html" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN body_html TEXT NOT NULL DEFAULT ''")
        if "sender_email" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN sender_email TEXT NOT NULL DEFAULT ''")
        if "variant" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
        for column in ("opened_at", "clicked_at"):
            if column not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN {} TEXT".format(column))
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_opened_at ON messages(opened_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_clicked_at ON messages(clicked_at)")
        for column, definition in (("template_id", "INTEGER"),
                                   ("template_name", "TEXT NOT NULL DEFAULT ''"),
                                   ("template_version", "INTEGER")):
            if column not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN {} {}".format(column, definition))
        contact_columns = [item["name"] for item in db.execute("PRAGMA table_info(contacts)")]
        if "merged_into_contact_id" not in contact_columns:
            db.execute("ALTER TABLE contacts ADD COLUMN merged_into_contact_id INTEGER REFERENCES contacts(id)")
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
        db.execute("""INSERT INTO delivery_events(message_id,kind,observed_at,detail)
                      SELECT id,'needs_review',?, '发送进程中断；请核对外部邮箱后手动处理'
                      FROM messages WHERE status='sending'""", (iso(utcnow()),))
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


def create_draft(path, name, messages, owner_email="__legacy_admin__", ab_mode=False):
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
        cur = db.execute("INSERT INTO batches(owner_email,name,state,created_at,ab_mode) VALUES (?,?,?,?,?)",
                         (owner_email, name[:120] or "未命名批次", "draft", iso(utcnow()), int(bool(ab_mode))))
        batch_id = cur.lastrowid
        db.executemany("""INSERT INTO messages(batch_id,row_number,recipient_email,subject,body,body_html,
                           template_id,template_name,template_version,variant) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       [(batch_id,) + tuple(row[:4]) + (row[4] if len(row) > 4 else "",
                        row[5] if len(row) > 5 else None, row[6] if len(row) > 6 else "",
                        row[7] if len(row) > 7 else None, row[8] if len(row) > 8 else "") for row in messages])
    return batch_id


def copy_batch(path, owner_email, source_id, name):
    """Copy the original content into a new draft, then recheck sending blocks."""
    with connect(path) as db:
        source = db.execute("SELECT name FROM batches WHERE id=? AND owner_email=?",
                            (source_id, owner_email)).fetchone()
        if not source:
            raise ValueError("批次不存在")
        rows = db.execute("""SELECT row_number,recipient_email,subject,body,body_html,
                                   template_id,template_name,template_version
                            FROM messages WHERE batch_id=? ORDER BY row_number""", (source_id,)).fetchall()
    if not rows:
        raise ValueError("原批次没有可复制的邮件")
    if len(rows) > MAX_ROWS:
        raise ValueError("原批次超过单批邮件上限")
    messages = [tuple(row) for row in rows]
    return create_draft(path, name.strip() or source["name"] + "（副本）", messages, owner_email)


def report_data(path, owner_email, start_date, end_date, offset_hours=8):
    """Counts distinct messages by the local date of each observed event."""
    if start_date > end_date or (end_date - start_date).days >= 366:
        raise ValueError("统计时间范围最多为 366 天")
    offset = dt.timedelta(hours=offset_hours)
    lower = iso((dt.datetime.combine(start_date, dt.time()) - offset).replace(tzinfo=dt.timezone.utc))
    upper = iso((dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time()) - offset).replace(tzinfo=dt.timezone.utc))
    fields = {"submitted": "submitted_at", "opened": "opened_at", "clicked": "clicked_at",
              "failed": "last_attempt_at", "bounced": "bounced_at", "replied": "replied_at"}
    with connect(path) as db:
        rows = db.execute("""SELECT m.batch_id,b.name,m.status,m.submitted_at,m.opened_at,m.clicked_at,
                                   m.last_attempt_at,m.bounced_at,m.replied_at
                            FROM messages m JOIN batches b ON b.id=m.batch_id
                            WHERE b.owner_email=? AND (
                              (m.submitted_at>=? AND m.submitted_at<?) OR
                              (m.opened_at>=? AND m.opened_at<?) OR
                              (m.clicked_at>=? AND m.clicked_at<?) OR
                              (m.last_attempt_at>=? AND m.last_attempt_at<?) OR
                              (m.bounced_at>=? AND m.bounced_at<?) OR
                              (m.replied_at>=? AND m.replied_at<?))""",
                          (owner_email,) + (lower, upper) * 6).fetchall()
    def empty():
        return {key: 0 for key in fields}
    totals, days, months, batches = empty(), {}, {}, {}
    for row in rows:
        for key, column in fields.items():
            value = row[column]
            if key == "failed" and row["status"] not in ("failed", "needs_review"):
                continue
            if not value or not lower <= value < upper:
                continue
            day = (parse_iso(value) + offset).date().isoformat()
            month = day[:7]
            batch = batches.setdefault(row["batch_id"], {"id": row["batch_id"], "name": row["name"], **empty()})
            totals[key] += 1
            days.setdefault(day, empty())[key] += 1
            months.setdefault(month, empty())[key] += 1
            batch[key] += 1
    return {"totals": totals, "days": sorted(days.items(), reverse=True),
            "months": sorted(months.items(), reverse=True),
            "batches": sorted(batches.values(), key=lambda item: (-item["submitted"], item["id"]))}


def ab_click_guidance(groups, state, track_click, latest_submitted_at, now=None):
    """Give a conservative, non-automatic A/B click comparison."""
    if not track_click:
        return None, "未启用点击记录；可以比较提交、退信和回复，暂不提供点击建议。"
    if state != "completed":
        return None, "任务尚未完成，待两组发送结束后再比较。"
    if len(groups) != 2 or any(group["submitted"] < 30 for group in groups):
        return None, "每组至少需要 30 封已提交邮件，当前不提供建议。"
    now = now or utcnow()
    if not latest_submitted_at or parse_iso(latest_submitted_at) > now - dt.timedelta(hours=24):
        return None, "最后一封提交后需观察至少 24 小时，当前不提供建议。"

    def wilson(clicks, submitted):
        if not 0 <= clicks <= submitted:
            raise ValueError("A/B 点击统计无效")
        z = 1.96
        proportion = clicks / submitted
        denominator = 1 + z * z / submitted
        center = (proportion + z * z / (2 * submitted)) / denominator
        radius = z * math.sqrt(proportion * (1 - proportion) / submitted + z * z / (4 * submitted * submitted)) / denominator
        return center - radius, center + radius

    first = wilson(groups[0]["clicked"], groups[0]["submitted"])
    second = wilson(groups[1]["clicked"], groups[1]["submitted"])
    if first[0] > second[1]:
        return "A", "A 组的观测点击率区间高于 B 组；后续任务可优先考虑 A 模板。"
    if second[0] > first[1]:
        return "B", "B 组的观测点击率区间高于 A 组；后续任务可优先考虑 B 模板。"
    return None, "两组观测点击率差异仍不明确；暂不建议选定模板。"


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


def copy_template(path, owner_email, template_id, name, version=None):
    """Create an independent v1 copy of an owned template version."""
    source = get_template(path, owner_email, template_id, version)
    return save_template(path, owner_email, name, source["subject"], source["body_text"], source["body_html"])


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


class _TemplateHtmlInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.images = []
        self.unsupported = set()
        self.event_attributes = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if any(name.startswith("on") for name in attributes):
            self.event_attributes = True
        if tag in ("script", "form", "iframe", "object", "embed"):
            self.unsupported.add(tag)
        if tag == "a":
            self.links.append(attributes.get("href") or "")
        if tag == "img":
            self.images.append(attributes)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def preflight_template(template, contact):
    """Check one rendered template without requesting URLs or sending mail."""
    try:
        subject, text, body_html = render_template(template, contact)
    except ValueError as exc:
        return [str(exc)], []
    errors, warnings = [], []
    if any("{{" in value or "}}" in value for value in (subject, text, body_html)):
        errors.append("展开后仍有疑似未填的 {{变量}}，请检查拼写与括号。")
    if len(subject) > 78:
        warnings.append("主题超过 78 个字符，部分邮箱界面可能截断显示。")
    if body_html:
        inspector = _TemplateHtmlInspector()
        inspector.feed(body_html)
        if inspector.unsupported:
            errors.append("HTML 含有邮件客户端通常不支持的标签：{}。".format(", ".join(sorted(inspector.unsupported))))
        if inspector.event_attributes:
            errors.append("HTML 含有交互事件属性，邮件客户端通常不会执行，请检查内容。")
        for target in inspector.links:
            target = target.strip()
            try:
                parsed = urlparse(target)
            except ValueError:
                errors.append("HTML 含空链接、相对链接或不支持的链接地址，请逐项检查。")
                continue
            if parsed.scheme in ("http", "https") and _safe_click_target(target):
                if parsed.scheme == "http":
                    warnings.append("存在非 HTTPS 链接，建议检查目标站点是否支持 HTTPS。")
            elif parsed.scheme == "mailto" and EMAIL_RE.fullmatch(parsed.path):
                pass
            else:
                errors.append("HTML 含空链接、相对链接或不支持的链接地址，请逐项检查。")
        if inspector.images:
            warnings.append("HTML 含图片；预览不会加载外部图片，部分收件客户端也可能默认阻止显示。")
            if any(not (item.get("alt") or "").strip() for item in inspector.images):
                warnings.append("有图片缺少替代文字，建议填写 alt 说明。")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def _like_literal(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def select_campaign_contacts(path, owner_email, filters=None):
    filters = dict(filters or {})
    included = str(filters.get("include_category", "") or "").strip()
    excluded = str(filters.get("exclude_category", "") or "").strip()
    contains = str(filters.get("email_contains", "") or "").strip().lower()
    excludes = str(filters.get("email_excludes", "") or "").strip().lower()
    if any(len(value) > 100 for value in (included, excluded, contains, excludes)):
        raise ValueError("筛选条件不能超过 100 个字符")
    if included and included == excluded:
        raise ValueError("包含分类和排除分类不能相同")
    try:
        sent_days = int(filters.get("exclude_sent_days", 0) or 0)
        opened_days = int(filters.get("opened_days", 0) or 0)
        clicked_days = int(filters.get("clicked_days", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("行为筛选天数无效")
    if any(not 0 <= value <= 365 for value in (sent_days, opened_days, clicked_days)):
        raise ValueError("行为筛选天数需为 0 至 365")
    query = """SELECT c.* FROM contacts c WHERE c.owner_email=? AND c.status='subscribed' AND c.deleted_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.recipient_email=c.email AND s.owner_email IN (c.owner_email,'*'))"""
    params = [owner_email]
    if included:
        query += " AND EXISTS (SELECT 1 FROM contact_category_links l JOIN contact_categories g ON g.id=l.category_id WHERE l.contact_id=c.id AND g.owner_email=? AND g.name=?)"
        params += [owner_email, included]
    if excluded:
        query += " AND NOT EXISTS (SELECT 1 FROM contact_category_links l JOIN contact_categories g ON g.id=l.category_id WHERE l.contact_id=c.id AND g.owner_email=? AND g.name=?)"
        params += [owner_email, excluded]
    if contains:
        query += " AND c.email LIKE ? ESCAPE '\\'"
        params.append(_like_literal(contains))
    if excludes:
        query += " AND c.email NOT LIKE ? ESCAPE '\\'"
        params.append(_like_literal(excludes))
    if sent_days:
        query += """ AND NOT EXISTS (SELECT 1 FROM messages m JOIN batches b ON b.id=m.batch_id
                     WHERE b.owner_email=c.owner_email AND lower(m.recipient_email)=c.email AND m.submitted_at>=?)"""
        params.append(iso(utcnow() - dt.timedelta(days=sent_days)))
    for days, column in ((opened_days, "opened_at"), (clicked_days, "clicked_at")):
        if days:
            query += """ AND EXISTS (SELECT 1 FROM messages m JOIN batches b ON b.id=m.batch_id
                         WHERE b.owner_email=c.owner_email AND lower(m.recipient_email)=c.email
                         AND m.{}>=?)""".format(column)
            params.append(iso(utcnow() - dt.timedelta(days=days)))
    with connect(path) as db:
        contacts = db.execute(query + " ORDER BY c.id LIMIT ?", params + [MAX_ROWS + 1]).fetchall()
    if not contacts or len(contacts) > MAX_ROWS:
        raise ValueError("筛选结果为空或超过每批 1000 人")
    return contacts


def prepare_contact_campaign(path, owner_email, template_ids, filters=None):
    filters = dict(filters or {})
    ids = template_ids if isinstance(template_ids, (list, tuple)) else [template_ids]
    try:
        ids = [int(value) for value in ids]
    except (TypeError, ValueError):
        raise ValueError("请选择有效的邮件模板")
    if not 1 <= len(ids) <= 5 or any(value < 1 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("请按顺序选择 1 至 5 个不同的邮件模板")
    ab_mode = str(filters.get("ab_mode", "")).lower() in ("1", "true")
    if ab_mode and len(ids) != 2:
        raise ValueError("A/B 对比必须恰好选择 2 个模板")
    templates = [get_template(path, owner_email, template_id) for template_id in ids]
    contacts = select_campaign_contacts(path, owner_email, filters)
    if ab_mode and len(contacts) < 2:
        raise ValueError("A/B 对比至少需要 2 位可发送联系人")
    variants = {}
    if ab_mode:
        seed = json.dumps([owner_email, ids, filters.get("name", "")], ensure_ascii=False)
        ranked = sorted(range(len(contacts)), key=lambda index: hashlib.sha256(
            (seed + "\0" + contacts[index]["email"]).encode("utf-8")).digest())
        variants = {index: position % 2 for position, index in enumerate(ranked)}
    messages = []
    for index, contact in enumerate(contacts):
        number = index + 1
        template_index = variants[index] if ab_mode else index % len(templates)
        template = templates[template_index]
        try:
            subject, text, body_html = render_template(template, contact)
        except ValueError as exc:
            raise ValueError("{}：{}".format(contact["email"], exc))
        messages.append((number, contact["email"], subject, text, body_html,
                         template["id"], template["name"], template["version"],
                         "AB"[template_index] if ab_mode else ""))
    return messages


def create_contact_campaign(path, owner_email, name, template_id, category="", filters=None):
    filters = dict(filters or {})
    if category:
        filters["include_category"] = category
    messages = prepare_contact_campaign(path, owner_email, template_id, filters)
    ab_mode = str(filters.get("ab_mode", "")).lower() in ("1", "true")
    return create_draft(path, name, messages, owner_email, ab_mode)


def send_template_test(path, owner_email, template_id, contact_id, recipient, settings, version=None):
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
    subject, text, body_html = render_template(get_template(path, owner_email, template_id, version), contact)
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
        db.execute("INSERT OR IGNORE INTO suppressions(owner_email,recipient_email,reason,created_at) VALUES (?,?,?,?)",
                   (owner_email, recipient, reason[:200], iso(utcnow())))
        db.execute("""UPDATE contacts SET status='unsubscribed',status_reason=?,updated_at=?
                      WHERE email=? AND status='subscribed' AND (?='*' OR owner_email=?)""",
                   (reason[:200], iso(utcnow()), recipient, owner_email, owner_email))
        db.execute("""UPDATE messages SET status='suppressed',last_error='已加入不再联系名单'
                      WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                      (SELECT id FROM batches WHERE owner_email=?)""", (recipient, owner_email))
        if reason == "收件人退订" and owner_email != "*":
            event_key = "unsubscribe:" + hashlib.sha256(recipient.encode("utf-8")).hexdigest()
            _apply_automation_event(db, owner_email, recipient, "unsubscribe", event_key)


def remove_manual_suppression(path, owner_email, recipient):
    recipient = str(recipient or "").strip().lower()
    if not EMAIL_RE.fullmatch(recipient):
        raise ValueError("邮箱格式无效")
    with connect(path) as db:
        removed = db.execute("""DELETE FROM suppressions WHERE owner_email=? AND recipient_email=?
                                AND reason='手动添加'""", (owner_email, recipient)).rowcount
        if not removed:
            raise ValueError("只能移除本账号手动添加的地址")


AUTOMATION_EVENTS = {"open": "观测打开", "click": "观测点击", "unsubscribe": "收件人退订", "birthday": "联系人生日"}


def save_automation_rule(path, owner_email, event_kind, category_name, followup_template_id=None):
    category_name = str(category_name or "").strip()
    if event_kind not in AUTOMATION_EVENTS:
        raise ValueError("请选择支持的触发行为")
    if not category_name or len(category_name) > 80 or re.search(r"[,，;；\x00-\x1f]", category_name):
        raise ValueError("分类名称需为 1 至 80 个字符，且不能包含逗号或分号")
    followup_template_id = int(followup_template_id or 0) or None
    if followup_template_id and event_kind == "unsubscribe":
        raise ValueError("退订行为不能生成后续邮件草稿")
    if event_kind == "birthday" and not followup_template_id:
        raise ValueError("生日规则必须选择邮件模板")
    with connect(path) as db:
        if followup_template_id and not db.execute(
                "SELECT 1 FROM templates WHERE id=? AND owner_email=?", (followup_template_id, owner_email)).fetchone():
            raise ValueError("只能选择自己的邮件模板")
        count = db.execute("SELECT COUNT(*) FROM automation_rules WHERE owner_email=?", (owner_email,)).fetchone()[0]
        if count >= 20:
            raise ValueError("每个账号最多创建 20 条自动分类规则")
        if db.execute("""SELECT 1 FROM automation_rules WHERE owner_email=? AND event_kind=? AND category_name=?""",
                      (owner_email, event_kind, category_name)).fetchone():
            raise ValueError("相同的自动分类规则已存在")
        db.execute("INSERT OR IGNORE INTO contact_categories(owner_email,name) VALUES (?,?)", (owner_email, category_name))
        try:
            return db.execute("""INSERT INTO automation_rules(owner_email,event_kind,category_name,
                                 followup_template_id,created_at) VALUES (?,?,?,?,?)""",
                              (owner_email, event_kind, category_name, followup_template_id, iso(utcnow()))).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("相同的自动分类规则已存在")


def set_automation_rule(path, owner_email, rule_id, enabled):
    with connect(path) as db:
        changed = db.execute("UPDATE automation_rules SET enabled=? WHERE id=? AND owner_email=?",
                             (int(bool(enabled)), rule_id, owner_email)).rowcount
        if not changed:
            raise ValueError("自动分类规则不存在")


def delete_automation_rule(path, owner_email, rule_id):
    with connect(path) as db:
        changed = db.execute("DELETE FROM automation_rules WHERE id=? AND owner_email=?",
                             (rule_id, owner_email)).rowcount
        if not changed:
            raise ValueError("自动分类规则不存在")


def _create_review_draft(db, owner_email, contact, template_id, name):
    """Create one owned review-only draft in the current transaction."""
    template = db.execute("""SELECT t.id,t.name,v.version,v.subject,v.body_text,v.body_html
                             FROM templates t JOIN template_versions v ON v.template_id=t.id
                             WHERE t.id=? AND t.owner_email=? AND v.version=(
                               SELECT MAX(version) FROM template_versions WHERE template_id=t.id)""",
                          (template_id, owner_email)).fetchone()
    reason = ""
    if contact["status"] != "subscribed" or db.execute(
            "SELECT 1 FROM suppressions WHERE recipient_email=? AND owner_email IN (?, '*')",
            (contact["email"], owner_email)).fetchone():
        reason = "联系人已退订或位于不再联系名单"
    elif not template:
        reason = "模板已不存在"
    else:
        try:
            subject, body, body_html = render_template(template, contact)
        except ValueError as exc:
            reason = str(exc)[:300]
    batch_id = None
    if not reason:
        batch_id = db.execute("INSERT INTO batches(owner_email,name,state,created_at) VALUES (?,?, 'draft',?)",
                              (owner_email, ("自动草稿：" + name)[:120], iso(utcnow()))).lastrowid
        db.execute("""INSERT INTO messages(batch_id,row_number,recipient_email,subject,body,body_html,
                      template_id,template_name,template_version) VALUES (?,?,?,?,?,?,?,?,?)""",
                   (batch_id, 1, contact["email"], subject, body, body_html,
                    template["id"], template["name"], template["version"]))
    return batch_id, reason


def _create_automation_draft(db, owner_email, contact, rule, event_key):
    """Create one reviewable draft in the event transaction; never queue or send it."""
    if not rule["followup_template_id"]:
        return
    batch_id, reason = _create_review_draft(db, owner_email, contact,
                                            rule["followup_template_id"], rule["category_name"])
    db.execute("""INSERT INTO automation_drafts(rule_id,event_key,contact_id,batch_id,status,detail,created_at)
                  VALUES (?,?,?,?,?,?,?)""", (rule["id"], event_key, contact["id"], batch_id,
                                             "draft" if batch_id else "skipped", reason, iso(utcnow())))


def _apply_automation_event(db, owner_email, recipient_email, event_kind, event_key):
    contact = db.execute("""SELECT * FROM contacts WHERE owner_email=? AND email=? AND deleted_at IS NULL
                            AND (status='subscribed' OR ?='unsubscribe')""",
                         (owner_email, recipient_email.lower(), event_kind)).fetchone()
    if not contact:
        return 0
    applied = 0
    rules = db.execute("""SELECT id,category_name,followup_template_id FROM automation_rules
                          WHERE owner_email=? AND event_kind=? AND enabled=1""",
                       (owner_email, event_kind)).fetchall()
    for rule in rules:
        inserted = db.execute("""INSERT OR IGNORE INTO automation_runs(rule_id,event_key,contact_id,applied_at)
                                 VALUES (?,?,?,?)""",
                              (rule["id"], event_key, contact["id"], iso(utcnow()))).rowcount
        if not inserted:
            continue
        db.execute("INSERT OR IGNORE INTO contact_categories(owner_email,name) VALUES (?,?)",
                   (owner_email, rule["category_name"]))
        category_id = db.execute("""SELECT id FROM contact_categories WHERE owner_email=? AND name=?""",
                                 (owner_email, rule["category_name"])).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO contact_category_links(contact_id,category_id) VALUES (?,?)",
                   (contact["id"], category_id))
        _create_automation_draft(db, owner_email, contact, rule, event_key)
        applied += 1
    return applied


def run_birthday_automations(path, now=None, offset_hours=8):
    """Generate review-only drafts once per local birthday and rule, including after a restart."""
    now = now or utcnow()
    local_date = (now + dt.timedelta(hours=offset_hours)).date()
    month_day = local_date.strftime("%m-%d")
    created = 0
    with connect(path) as db:
        contacts = db.execute("""SELECT c.id,c.owner_email,c.email,c.birthday FROM contacts c
                                 WHERE c.status='subscribed' AND c.deleted_at IS NULL
                                 AND length(c.birthday)=10 AND substr(c.birthday,6,5)=?
                                 AND EXISTS (SELECT 1 FROM automation_rules r WHERE
                                   r.owner_email=c.owner_email AND r.event_kind='birthday' AND r.enabled=1)
                                 ORDER BY c.id""", (month_day,)).fetchall()
        for contact in contacts:
            try:
                dt.datetime.strptime(contact["birthday"], "%Y-%m-%d")
            except ValueError:
                continue
            key = "birthday:{}:{}".format(local_date.isoformat(), contact["id"])
            created += _apply_automation_event(db, contact["owner_email"], contact["email"], "birthday", key)
    return created


SCHEDULE_KINDS = {"holiday": "每年指定日期", "weekly": "每周", "monthly": "每月"}


def save_scheduled_rule(path, owner_email, kind, schedule_value, target_category, template_id):
    schedule_value = str(schedule_value or "").strip()
    target_category = str(target_category or "").strip()
    template_id = int(template_id or 0)
    if kind not in SCHEDULE_KINDS:
        raise ValueError("请选择支持的循环类型")
    if kind == "holiday":
        if not re.fullmatch(r"\d{2}-\d{2}", schedule_value):
            raise ValueError("指定日期请填写 MM-DD，例如 01-01")
        try:
            dt.datetime.strptime("2000-" + schedule_value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("指定日期不存在")
    elif kind == "weekly" and schedule_value not in {str(i) for i in range(1, 8)}:
        raise ValueError("每周日期须为 1 至 7，周一为 1")
    elif kind == "monthly" and schedule_value not in {str(i) for i in range(1, 32)}:
        raise ValueError("每月日期须为 1 至 31")
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM contact_categories WHERE owner_email=? AND name=?",
                          (owner_email, target_category)).fetchone():
            raise ValueError("请选择已有的联系人分类")
        if not db.execute("SELECT 1 FROM templates WHERE id=? AND owner_email=?",
                          (template_id, owner_email)).fetchone():
            raise ValueError("只能选择自己的邮件模板")
        if db.execute("SELECT COUNT(*) FROM scheduled_rules WHERE owner_email=?", (owner_email,)).fetchone()[0] >= 20:
            raise ValueError("每个账号最多创建 20 条定时规则")
        try:
            return db.execute("""INSERT INTO scheduled_rules(owner_email,kind,schedule_value,target_category,
                                 template_id,created_at) VALUES (?,?,?,?,?,?)""",
                              (owner_email, kind, schedule_value, target_category, template_id, iso(utcnow()))).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("相同的定时规则已存在")


def set_scheduled_rule(path, owner_email, rule_id, enabled):
    with connect(path) as db:
        if not db.execute("UPDATE scheduled_rules SET enabled=? WHERE id=? AND owner_email=?",
                          (int(bool(enabled)), rule_id, owner_email)).rowcount:
            raise ValueError("定时规则不存在")


def delete_scheduled_rule(path, owner_email, rule_id):
    with connect(path) as db:
        if not db.execute("DELETE FROM scheduled_rules WHERE id=? AND owner_email=?",
                          (rule_id, owner_email)).rowcount:
            raise ValueError("定时规则不存在")


def run_scheduled_automations(path, now=None, offset_hours=8):
    """Draft for due category members once per local date; short months do not shift a monthly date."""
    local_date = ((now or utcnow()) + dt.timedelta(hours=offset_hours)).date()
    created = 0
    with connect(path) as db:
        rules = db.execute("SELECT * FROM scheduled_rules WHERE enabled=1 ORDER BY id").fetchall()
        for rule in rules:
            due = ((rule["kind"] == "holiday" and rule["schedule_value"] == local_date.strftime("%m-%d")) or
                   (rule["kind"] == "weekly" and rule["schedule_value"] == str(local_date.isoweekday())) or
                   (rule["kind"] == "monthly" and rule["schedule_value"] == str(local_date.day)))
            if not due:
                continue
            contacts = db.execute("""SELECT c.* FROM contacts c JOIN contact_category_links l ON l.contact_id=c.id
                                     JOIN contact_categories g ON g.id=l.category_id
                                     WHERE c.owner_email=? AND c.status='subscribed' AND c.deleted_at IS NULL
                                     AND g.owner_email=? AND g.name=? AND NOT EXISTS (
                                       SELECT 1 FROM scheduled_runs s WHERE s.rule_id=?
                                       AND s.occurrence_date=? AND s.contact_id=c.id)
                                     ORDER BY c.id LIMIT 100""",
                                  (rule["owner_email"], rule["owner_email"], rule["target_category"],
                                   rule["id"], local_date.isoformat())).fetchall()
            for contact in contacts:
                batch_id, reason = _create_review_draft(db, rule["owner_email"], contact,
                                                        rule["template_id"], rule["target_category"])
                db.execute("""INSERT INTO scheduled_runs(rule_id,occurrence_date,contact_id,batch_id,status,detail,created_at)
                              VALUES (?,?,?,?,?,?,?)""", (rule["id"], local_date.isoformat(), contact["id"],
                                                          batch_id, "draft" if batch_id else "skipped", reason, iso(utcnow())))
                created += 1
    return created


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


def record_audit_event(path, owner_email, action, target_id=None):
    with connect(path) as db:
        db.execute("INSERT INTO audit_events(owner_email,action,target_id,created_at) VALUES (?,?,?,?)",
                   (owner_email, action, target_id, iso(utcnow())))


def notification_preferences(path, owner_email):
    with connect(path) as db:
        row = db.execute("SELECT failures_enabled,bounces_enabled FROM notification_preferences WHERE owner_email=?",
                         (owner_email,)).fetchone()
    return (bool(row["failures_enabled"]), bool(row["bounces_enabled"])) if row else (True, True)


def save_notification_preferences(path, owner_email, failures_enabled, bounces_enabled):
    with connect(path) as db:
        db.execute("""INSERT OR REPLACE INTO notification_preferences(owner_email,failures_enabled,bounces_enabled)
                      VALUES (?,?,?)""",
                   (owner_email, int(bool(failures_enabled)), int(bool(bounces_enabled))))


def list_notifications(path, owner_email, limit=100):
    failures, bounces = notification_preferences(path, owner_email)
    if not failures and not bounces:
        return []
    kinds = (["failed", "needs_review"] if failures else []) + (["bounce"] if bounces else [])
    placeholders = ",".join("?" for _ in kinds)
    with connect(path) as db:
        return db.execute("""SELECT e.id,e.kind,e.observed_at,b.id AS batch_id,b.name AS batch_name,
                            m.recipient_email,r.read_at FROM delivery_events e
                            JOIN messages m ON m.id=e.message_id JOIN batches b ON b.id=m.batch_id
                            LEFT JOIN notification_reads r ON r.event_id=e.id AND r.owner_email=b.owner_email
                            WHERE b.owner_email=? AND e.kind IN ({}) ORDER BY e.id DESC LIMIT ?""".format(placeholders),
                          (owner_email, *kinds, min(max(int(limit), 1), 100))).fetchall()


def mark_notification_read(path, owner_email, event_id):
    with connect(path) as db:
        row = db.execute("""SELECT 1 FROM delivery_events e JOIN messages m ON m.id=e.message_id
                            JOIN batches b ON b.id=m.batch_id WHERE e.id=? AND b.owner_email=?
                            AND e.kind IN ('failed','needs_review','bounce')""", (event_id, owner_email)).fetchone()
        if not row:
            raise ValueError("提醒不存在")
        db.execute("INSERT OR IGNORE INTO notification_reads(owner_email,event_id,read_at) VALUES (?,?,?)",
                   (owner_email, event_id, iso(utcnow())))


def parse_provider_feedback_upload(filename, data):
    if not filename.lower().endswith(".csv"):
        raise ValueError("反馈文件仅支持 UTF-8 CSV")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("反馈文件为空或超过 5 MB")
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error):
        raise ValueError("反馈 CSV 格式无效，请使用 UTF-8 编码")
    rows = [(number, row) for number, row in enumerate(rows, 1) if any(cell.strip() for cell in row)]
    if len(rows) < 2 or len(rows) > 1001:
        raise ValueError("反馈文件需有表头，且每次最多 1000 条")
    headers = [cell.strip().lower() for cell in rows[0][1]]
    email_columns = [i for i, value in enumerate(headers) if value in ("邮箱", "email")]
    status_columns = [i for i, value in enumerate(headers) if value in ("类型", "status")]
    if len(email_columns) != 1 or len(status_columns) != 1:
        raise ValueError("反馈表头需包含且仅包含一个邮箱列和一个类型列")
    result, seen = [], set()
    statuses = {"投诉": "complained", "complained": "complained",
                "无效地址": "invalid", "invalid": "invalid"}
    for number, row in rows[1:]:
        if len(row) != len(headers):
            raise ValueError("第 {} 行列数与表头不一致".format(number))
        address = row[email_columns[0]].strip().lower()
        status = statuses.get(row[status_columns[0]].strip().lower())
        if not EMAIL_RE.fullmatch(address) or len(address) > 254:
            raise ValueError("第 {} 行邮箱无效".format(number))
        if not status:
            raise ValueError("第 {} 行类型仅支持投诉或无效地址".format(number))
        if address in seen:
            raise ValueError("第 {} 行邮箱重复".format(number))
        seen.add(address)
        result.append((number, address, status))
    return result


def import_provider_feedback(path, owner_email, source_name, filename, data):
    source_name = str(source_name or "").strip()
    if not source_name or len(source_name) > 80 or re.search(r"[\x00-\x1f]", source_name):
        raise ValueError("反馈来源需为 1 至 80 个字符")
    items = parse_provider_feedback_upload(filename, data)
    digest = hashlib.sha256(data).hexdigest()
    severity = {"subscribed": 0, "invalid": 1, "unsubscribed": 2, "complained": 3}
    now = iso(utcnow())
    with connect(path) as db:
        if db.execute("SELECT 1 FROM provider_feedback_imports WHERE owner_email=? AND file_sha256=?",
                      (owner_email, digest)).fetchone():
            return None
        import_id = db.execute("""INSERT INTO provider_feedback_imports
                                  (owner_email,source_name,file_sha256,row_count,created_at) VALUES (?,?,?,?,?)""",
                               (owner_email, source_name, digest, len(items), now)).lastrowid
        changed, stopped = 0, 0
        for number, address, status in items:
            reason = "{}反馈：{}".format(source_name, "投诉" if status == "complained" else "无效地址")[:200]
            row = db.execute("SELECT id,status FROM contacts WHERE owner_email=? AND email=?",
                             (owner_email, address)).fetchone()
            previous = row["status"] if row else ""
            if row:
                if severity[status] > severity[previous]:
                    db.execute("UPDATE contacts SET status=?,status_reason=?,updated_at=? WHERE id=?",
                               (status, reason, now, row["id"]))
                    changed += 1
            else:
                db.execute("""INSERT INTO contacts(owner_email,email,status,status_reason,created_at,updated_at)
                              VALUES (?,?,?,?,?,?)""", (owner_email, address, status, reason, now, now))
                changed += 1
            db.execute("""INSERT OR IGNORE INTO suppressions
                          (owner_email,recipient_email,reason,created_at) VALUES (?,?,?,?)""",
                       (owner_email, address, reason, now))
            stopped += db.execute("""UPDATE messages SET status='suppressed',last_error=?
                                     WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                                     (SELECT id FROM batches WHERE owner_email=?)""",
                                  (reason, address, owner_email)).rowcount
            db.execute("""INSERT INTO provider_feedback_events
                          (import_id,row_number,recipient_email,status,previous_status) VALUES (?,?,?,?,?)""",
                       (import_id, number, address, status, previous))
    return len(items), changed, stopped


def _category_name(value):
    name = str(value or "").strip()
    if not name or len(name) > 80 or re.search(r"[,，;；\x00-\x1f]", name):
        raise ValueError("分类名称需为 1 至 80 个字符，且不能包含逗号或分号")
    return name


def create_category(path, owner_email, name):
    name = _category_name(name)
    with connect(path) as db:
        if db.execute("SELECT COUNT(*) FROM contact_categories WHERE owner_email=?", (owner_email,)).fetchone()[0] >= 100:
            raise ValueError("每个账号最多创建 100 个分类")
        try:
            return db.execute("INSERT INTO contact_categories(owner_email,name) VALUES (?,?)",
                              (owner_email, name)).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("分类已存在")


def rename_category(path, owner_email, category_id, name):
    name = _category_name(name)
    with connect(path) as db:
        old = db.execute("SELECT name FROM contact_categories WHERE id=? AND owner_email=?",
                         (category_id, owner_email)).fetchone()
        if not old:
            raise ValueError("分类不存在")
        if old["name"] == name:
            return
        if db.execute("SELECT 1 FROM contact_categories WHERE owner_email=? AND name=?",
                      (owner_email, name)).fetchone():
            raise ValueError("目标分类已存在")
        db.execute("UPDATE contact_categories SET name=? WHERE id=?", (name, category_id))
        db.execute("UPDATE automation_rules SET category_name=? WHERE owner_email=? AND category_name=?",
                   (name, owner_email, old["name"]))
        db.execute("UPDATE scheduled_rules SET target_category=? WHERE owner_email=? AND target_category=?",
                   (name, owner_email, old["name"]))


def delete_category(path, owner_email, category_id):
    with connect(path) as db:
        row = db.execute("SELECT name FROM contact_categories WHERE id=? AND owner_email=?",
                         (category_id, owner_email)).fetchone()
        if not row:
            raise ValueError("分类不存在")
        if (db.execute("SELECT 1 FROM automation_rules WHERE owner_email=? AND category_name=?",
                       (owner_email, row["name"])).fetchone() or
                db.execute("SELECT 1 FROM scheduled_rules WHERE owner_email=? AND target_category=?",
                           (owner_email, row["name"])).fetchone()):
            raise ValueError("该分类正被自动化规则使用，请先删除相关规则")
        db.execute("DELETE FROM contact_categories WHERE id=?", (category_id,))


def set_contact_category(path, owner_email, contact_id, category_id, add):
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM contacts WHERE id=? AND owner_email=?", (contact_id, owner_email)).fetchone():
            raise ValueError("联系人不存在")
        if not db.execute("SELECT 1 FROM contact_categories WHERE id=? AND owner_email=?",
                          (category_id, owner_email)).fetchone():
            raise ValueError("分类不存在")
        if add:
            db.execute("INSERT OR IGNORE INTO contact_category_links(contact_id,category_id) VALUES (?,?)",
                       (contact_id, category_id))
        else:
            db.execute("DELETE FROM contact_category_links WHERE contact_id=? AND category_id=?",
                       (contact_id, category_id))


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
        row = db.execute("SELECT email,merged_into_contact_id FROM contacts WHERE id=? AND owner_email=?",
                         (contact_id, owner_email)).fetchone()
        if not row:
            raise ValueError("联系人不存在")
        if not deleted and row["merged_into_contact_id"]:
            raise ValueError("已合并的联系人不能直接恢复")
        changed = db.execute("UPDATE contacts SET deleted_at=?,updated_at=? WHERE id=? AND owner_email=?",
                             (iso(utcnow()) if deleted else None, iso(utcnow()), contact_id, owner_email))
        if deleted:
            db.execute("""UPDATE messages SET status='suppressed',last_error='联系人已移入回收站'
                          WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                          (SELECT id FROM batches WHERE owner_email=?)""", (row["email"], owner_email))


def merge_contacts(path, owner_email, primary_email, secondary_email):
    """Keep both address records, archive the secondary, and retain the strictest send block."""
    primary_email = str(primary_email or "").strip().lower()
    secondary_email = str(secondary_email or "").strip().lower()
    if not primary_email or not secondary_email or primary_email == secondary_email:
        raise ValueError("请填写两个不同的联系人邮箱")
    with connect(path) as db:
        primary = db.execute("SELECT * FROM contacts WHERE owner_email=? AND email=?",
                             (owner_email, primary_email)).fetchone()
        secondary = db.execute("SELECT * FROM contacts WHERE owner_email=? AND email=?",
                               (owner_email, secondary_email)).fetchone()
        if not primary or not secondary or primary["deleted_at"] or secondary["deleted_at"] or \
                primary["merged_into_contact_id"] or secondary["merged_into_contact_id"]:
            raise ValueError("两个邮箱都必须是当前账号下未回收、未合并的联系人")
        fields = ("name", "gender", "birthday", "phone", "region")
        merged_fields = [primary[key] or secondary[key] for key in fields]
        primary_custom = json.loads(primary["custom_json"])
        secondary_custom = json.loads(secondary["custom_json"])
        for key, value in secondary_custom.items():
            if not primary_custom.get(key) and value:
                primary_custom[key] = value
        severity = {"subscribed": 0, "invalid": 1, "unsubscribed": 2, "complained": 3}
        strict = secondary if severity[secondary["status"]] > severity[primary["status"]] else primary
        now = iso(utcnow())
        db.execute("""UPDATE contacts SET name=?,gender=?,birthday=?,phone=?,region=?,custom_json=?,
                      status=?,status_reason=?,updated_at=? WHERE id=?""",
                   tuple(merged_fields) + (json.dumps(primary_custom, ensure_ascii=False), strict["status"],
                                           strict["status_reason"], now, primary["id"]))
        db.execute("""INSERT OR IGNORE INTO contact_category_links(contact_id,category_id)
                      SELECT ?,category_id FROM contact_category_links WHERE contact_id=?""",
                   (primary["id"], secondary["id"]))
        db.execute("UPDATE contacts SET deleted_at=?,merged_into_contact_id=?,updated_at=? WHERE id=?",
                   (now, primary["id"], now, secondary["id"]))
        blocked = [secondary_email] + ([primary_email] if strict["status"] != "subscribed" else [])
        for address in blocked:
            db.execute("""UPDATE messages SET status='suppressed',last_error='联系人合并后禁止发送'
                          WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                          (SELECT id FROM batches WHERE owner_email=?)""", (address, owner_email))
    return primary["id"]


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
        db.execute("DELETE FROM sender_aliases WHERE owner_email=?", (owner_email,))
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


def verify_sender_alias(path, owner_email, alias_email, settings):
    """Ask SMTP whether this login may use an envelope sender; never issue DATA."""
    alias_email = str(alias_email or "").strip().lower()
    if (not REPLY_ADDRESS_RE.fullmatch(owner_email) or not REPLY_ADDRESS_RE.fullmatch(alias_email) or len(alias_email) > 254 or
            alias_email.startswith(".") or ".." in alias_email.split("@", 1)[0] or
            alias_email == owner_email.lower()):
        raise ValueError("请输入其他有效的发件邮箱地址")
    account = mailbox_settings(path, owner_email, settings)
    if not account or not all(account.get(key) for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")):
        raise ValueError("请先完成当前邮箱的 SMTP 授权")
    port = int(account.get("SMTP_PORT", 587))
    if port == 465:
        smtp = smtplib.SMTP_SSL(account["SMTP_HOST"], port, timeout=15, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(account["SMTP_HOST"], port, timeout=15)
    try:
        if port != 465:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(account["SMTP_USER"], account["SMTP_PASSWORD"])
        code, _ = smtp.mail(alias_email)
        if not 200 <= code < 300:
            raise ValueError("mailcow 未允许当前账号使用此发件地址")
        code, _ = smtp.rcpt(owner_email)
        if not 200 <= code < 300:
            raise ValueError("mailcow 未允许当前账号使用此发件地址")
        smtp.rset()
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()
    with connect(path) as db:
        db.execute("INSERT OR REPLACE INTO sender_aliases(owner_email,alias_email,verified_at) VALUES (?,?,?)",
                   (owner_email, alias_email, iso(utcnow())))
    return alias_email


def remove_sender_alias(path, owner_email, alias_email):
    with connect(path) as db:
        if not db.execute("DELETE FROM sender_aliases WHERE owner_email=? AND alias_email=?",
                          (owner_email, str(alias_email or "").strip().lower())).rowcount:
            raise ValueError("代发地址不存在")
        db.execute("""UPDATE batches SET state='paused' WHERE owner_email=? AND state='active'
                      AND id IN (SELECT batch_id FROM messages WHERE sender_email=? AND status='queued')""",
                   (owner_email, str(alias_email or "").strip().lower()))


def schedule_batch(path, batch_id, start_local, interval_seconds, offset_hours=8,
                   track_open=False, track_click=False, reply_to="", sender_email="", hourly_limit=None,
                   sender_emails=None):
    if not 60 <= interval_seconds <= 86400:
        raise ValueError("发送间隔需为 60 秒至 24 小时")
    if hourly_limit not in (None, ""):
        try:
            hourly_limit = int(hourly_limit)
        except (TypeError, ValueError):
            raise ValueError("批次每小时上限须为整数")
        if not 1 <= hourly_limit <= 1000:
            raise ValueError("批次每小时上限需为 1 至 1000 封")
    else:
        hourly_limit = None
    reply_to = str(reply_to or "").strip().lower()
    sender_email = str(sender_email or "").strip().lower()
    legacy_default = not sender_email and sender_emails is None
    selected_senders = [str(item or "").strip().lower() for item in (sender_emails or [sender_email])]
    if (not selected_senders or len(selected_senders) > 10 or len(selected_senders) != len(set(selected_senders)) or
            (not legacy_default and any(not REPLY_ADDRESS_RE.fullmatch(item) for item in selected_senders))):
        raise ValueError("请选择 1 至 10 个不重复的有效发件地址")
    if reply_to:
        local = reply_to.rsplit("@", 1)[0] if "@" in reply_to else ""
        valid = (len(reply_to) <= 254 and REPLY_ADDRESS_RE.fullmatch(reply_to) and
                 not local.startswith(".") and not local.endswith(".") and ".." not in local and
                 EMAIL_RE.fullmatch(sender_email) and
                 all(reply_to.rsplit("@", 1)[1] == item.rsplit("@", 1)[1] for item in selected_senders))
        if not valid:
            raise ValueError("回复地址需为发件邮箱同域的有效邮箱地址")
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
        batch = db.execute("SELECT state,owner_email FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["state"] != "draft":
            raise ValueError("这个批次已启动或不存在")
        if sender_email and batch["owner_email"] != "__legacy_admin__" and sender_email != batch["owner_email"]:
            raise ValueError("发件账号与批次所属账号不符")
        allowed = {sender_email}
        allowed.update(row[0] for row in db.execute("SELECT alias_email FROM sender_aliases WHERE owner_email=?",
                                                     (batch["owner_email"],)))
        if not set(selected_senders).issubset(allowed):
            raise ValueError("发件地址未通过当前账号的代发验证")
        rows = db.execute("SELECT id,body_html FROM messages WHERE batch_id=? ORDER BY row_number", (batch_id,)).fetchall()
        if (track_open or track_click) and any(not row["body_html"] for row in rows):
            raise ValueError("打开/点击跟踪只支持全部邮件均有 HTML 正文的批次")
        for index, row in enumerate(rows):
            due = iso(start + dt.timedelta(seconds=index * interval_seconds))
            db.execute("UPDATE messages SET status='queued',scheduled_at=?,next_attempt_at=?,sender_email=? WHERE id=?",
                       (due, due, selected_senders[index % len(selected_senders)], row["id"]))
        db.execute("UPDATE batches SET state='active',start_at=?,interval_seconds=?,track_open=?,track_click=?,reply_to=?,hourly_limit=? WHERE id=?",
                   (iso(start), interval_seconds, int(bool(track_open)), int(bool(track_click)), reply_to, hourly_limit, batch_id))


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


def tracking_ready(settings):
    return (settings.get("OAUTH_BASE_URL", "").startswith("https://") and
            len(settings.get("SESSION_SECRET", "")) >= 32)


def _safe_click_target(target):
    if (not isinstance(target, str) or len(target) > 2048 or "\\" in target or
            any(char.isspace() or ord(char) < 32 or ord(char) > 126 for char in target)):
        return False
    try:
        parsed = urlparse(target)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
        if parsed.scheme.lower() not in ("http", "https") or not host or "@" in parsed.netloc or port == 0:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def tracking_token(message_id, kind, secret, target=""):
    if (kind not in ("o", "c") or not secret or (kind == "c" and not _safe_click_target(target))
            or (kind == "o" and target)):
        raise ValueError("跟踪链接无效")
    payload = json.dumps([int(message_id), kind, target], separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), ("track:" + encoded).encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_tracking_token(token, kind, secret):
    if not secret or len(token) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", token):
        raise ValueError("跟踪链接无效")
    encoded, signature = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), ("track:" + encoded).encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("跟踪链接无效")
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("跟踪链接无效")
    if (not isinstance(payload, list) or len(payload) != 3 or type(payload[0]) is not int or payload[0] < 1
            or payload[1] != kind or (kind == "c" and not _safe_click_target(payload[2]))
            or (kind == "o" and payload[2] != "")):
        raise ValueError("跟踪链接无效")
    return payload[0], payload[2]


def _tracking_url(settings, kind, message_id, target=""):
    base = settings["OAUTH_BASE_URL"].rstrip("/") + settings.get("URL_PREFIX", "").rstrip("/")
    token = tracking_token(message_id, kind, settings["SESSION_SECRET"], target)
    return base + "/track/" + kind + "/" + token + (".gif" if kind == "o" else "")


class _TrackedHtml(HTMLParser):
    def __init__(self, settings, message_id):
        super(_TrackedHtml, self).__init__(convert_charrefs=False)
        self.settings, self.message_id, self.parts = settings, message_id, []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a" or not any(key.lower() == "href" and _safe_click_target(value) for key, value in attrs):
            self.parts.append(self.get_starttag_text())
            return
        pieces = []
        for key, value in attrs:
            if key.lower() == "href" and _safe_click_target(value):
                value = _tracking_url(self.settings, "c", self.message_id, value)
            pieces.append(" " + key + ("" if value is None else '="{}"'.format(html.escape(value, quote=True))))
        self.parts.append("<" + tag + "".join(pieces) + ">")

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append("&" + name + ";")

    def handle_charref(self, name):
        self.parts.append("&#" + name + ";")

    def handle_comment(self, data):
        self.parts.append("<!--" + data + "-->")

    def handle_decl(self, decl):
        self.parts.append("<!" + decl + ">")


def tracked_html(body_html, message_id, settings, track_open, track_click):
    if track_click:
        parser = _TrackedHtml(settings, message_id)
        parser.feed(body_html)
        parser.close()
        body_html = "".join(parser.parts)
    if track_open:
        pixel = '<img src="{}" width="1" height="1" alt="" style="display:none">'.format(
            html.escape(_tracking_url(settings, "o", message_id), quote=True))
        if re.search(r"</body\s*>", body_html, flags=re.I):
            body_html = re.sub(r"</body\s*>", lambda match: pixel + match.group(0), body_html, count=1, flags=re.I)
        else:
            body_html += pixel
    return body_html


def record_tracking_event(path, message_id, kind):
    if kind not in ("o", "c"):
        return False
    field, flag = ("opened_at", "track_open") if kind == "o" else ("clicked_at", "track_click")
    with connect(path) as db:
        row = db.execute("""SELECT m.status,m.opened_at,m.clicked_at,m.recipient_email,
                            b.owner_email,b.track_open,b.track_click FROM messages m
                            JOIN batches b ON b.id=m.batch_id WHERE m.id=?""", (message_id,)).fetchone()
        if not row or not row[flag] or row["status"] not in ("sending", "submitted"):
            return False
        if row[field]:
            return True
        now = iso(utcnow())
        changed = db.execute("UPDATE messages SET {}=? WHERE id=? AND {} IS NULL".format(field, field),
                             (now, message_id)).rowcount
        if changed:
            event_kind = "open" if kind == "o" else "click"
            db.execute("INSERT INTO delivery_events(message_id,kind,observed_at,detail) VALUES (?,?,?,?)",
                       (message_id, event_kind, now, "观测到首次打开" if kind == "o" else "观测到首次点击"))
            _apply_automation_event(db, row["owner_email"], row["recipient_email"],
                                    event_kind, "{}:{}".format(event_kind, message_id))
    return True


def _smtp_submit(row, settings):
    sender = (row["sender_email"] if "sender_email" in row.keys() and row["sender_email"]
              else settings["SMTP_FROM"])
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = row["recipient_email"]
    if "reply_to" in row.keys() and row["reply_to"]:
        msg["Reply-To"] = row["reply_to"]
    msg["Subject"] = row["subject"]
    msg["Date"] = email.utils.format_datetime(utcnow())
    message_id = "<{}@{}>".format(uuid.uuid4().hex, sender.rsplit("@", 1)[1])
    msg["Message-ID"] = message_id
    link = unsubscribe_url(settings, row["owner_email"], row["recipient_email"]) if "owner_email" in row.keys() else ""
    msg.set_content(row["body"] + ("\n\n不再接收此类邮件：" + link if link else ""))
    if "body_html" in row.keys() and row["body_html"]:
        footer = ('<p>不再接收此类邮件：<a href="{0}">退订</a></p>'.format(html.escape(link, quote=True)) if link else '')
        content = row["body_html"]
        if "id" in row.keys() and tracking_ready(settings) and (row["track_open"] or row["track_click"]):
            content = tracked_html(content, row["id"], settings, row["track_open"], row["track_click"])
        msg.add_alternative(content + footer, subtype="html")
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
        row, sender_settings = None, None
        cursor_time, cursor_id = "", 0
        capped_batches = set()
        blocked_owners = set()
        while not row:
            candidates = db.execute("""SELECT m.*,b.owner_email,b.track_open,b.track_click,b.reply_to,b.hourly_limit
                                       FROM messages m JOIN batches b ON b.id=m.batch_id
                                       WHERE m.status='queued' AND b.state='active' AND m.next_attempt_at<=?
                                       AND (m.next_attempt_at>? OR (m.next_attempt_at=? AND m.id>?))
                                       ORDER BY m.next_attempt_at,m.id LIMIT 100""",
                                    (iso(now), cursor_time, cursor_time, cursor_id)).fetchall()
            if not candidates:
                break
            for candidate in candidates:
                if candidate["batch_id"] in capped_batches:
                    continue
                owner = candidate["owner_email"]
                if owner in blocked_owners:
                    continue
                account = mailbox_settings(path, owner, settings)
                if not account or not all(account.get(key) for key in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")):
                    blocked_owners.add(owner)
                    continue
                last = db.execute("""SELECT MAX(m.last_attempt_at) AS value FROM messages m
                                     JOIN batches b ON b.id=m.batch_id WHERE b.owner_email=?""", (owner,)).fetchone()["value"]
                if last and parse_iso(last) + dt.timedelta(seconds=int(settings.get("MIN_INTERVAL_SECONDS", 60))) > now:
                    blocked_owners.add(owner)
                    continue
                sent_today = db.execute("""SELECT COUNT(*) AS n FROM messages m JOIN batches b ON b.id=m.batch_id
                                           WHERE b.owner_email=? AND m.submitted_at>=?""", (owner, iso(day_start))).fetchone()["n"]
                if sent_today >= int(settings.get("DAILY_LIMIT", 100)):
                    blocked_owners.add(owner)
                    continue
                hour_start = iso(now - dt.timedelta(hours=1))
                sent_hour = db.execute("""SELECT COUNT(*) AS n FROM messages m JOIN batches b ON b.id=m.batch_id
                                          WHERE b.owner_email=? AND m.submitted_at>=?""", (owner, hour_start)).fetchone()["n"]
                if sent_hour >= int(settings.get("HOURLY_LIMIT", 20)):
                    blocked_owners.add(owner)
                    continue
                if candidate["hourly_limit"] is not None:
                    sent_batch_hour = db.execute("""SELECT COUNT(*) AS n FROM messages
                                                    WHERE batch_id=? AND submitted_at>=?""",
                                                 (candidate["batch_id"], hour_start)).fetchone()["n"]
                    if sent_batch_hour >= candidate["hourly_limit"]:
                        capped_batches.add(candidate["batch_id"])
                        continue
                row, sender_settings = candidate, account
                break
            cursor_time, cursor_id = candidates[-1]["next_attempt_at"], candidates[-1]["id"]
        if not row:
            return False
        db.execute("UPDATE messages SET status='sending', attempt_count=attempt_count+1, last_attempt_at=? WHERE id=? AND status='queued'",
                   (iso(now), row["id"]))
        db.execute("INSERT INTO delivery_events(message_id,kind,observed_at,detail) VALUES (?,'attempt',?,?)",
                   (row["id"], iso(now), "第 {} 次提交 SMTP".format(row["attempt_count"] + 1)))
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
            db.execute("INSERT INTO delivery_events(message_id,kind,smtp_code,observed_at,detail) VALUES (?,?,?,?,?)",
                       (row["id"], "retry" if retry else "failed", int(code) if code is not None else None,
                        iso(utcnow()), error))
    else:
        with connect(path) as db:
            db.execute("UPDATE messages SET status='submitted',submitted_at=?,message_id=?,last_error=NULL WHERE id=?",
                       (iso(utcnow()), message_id, row["id"]))
            db.execute("INSERT INTO delivery_events(message_id,kind,observed_at,detail) VALUES (?,'submitted',?,?)",
                       (row["id"], iso(utcnow()), "SMTP 已接受提交"))
    return True


def _extract_dsn_feedback(message):
    for part in message.walk():
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            for block in payload if isinstance(payload, list) else []:
                raw = block.get("Final-Recipient") or block.get("Original-Recipient")
                if raw:
                    recipient = raw.split(";", 1)[-1].strip().lower()
                    if EMAIL_RE.fullmatch(recipient):
                        permanent = (str(block.get("Action", "")).lower() == "failed" and
                                     str(block.get("Status", "")).startswith("5."))
                        return recipient, permanent
    raw = message.get("X-Failed-Recipients")
    recipient = raw.strip().lower() if raw else None
    return (recipient, False) if recipient and EMAIL_RE.fullmatch(recipient) else (None, False)


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
            recipient, permanent_failure = _extract_dsn_feedback(msg)
            kind = "bounce" if recipient else "reply"
            matched = None
            with connect(path) as db:
                source_uid = owner_email + ":" + str(uid)
                already_seen = db.execute("SELECT 1 FROM imap_events WHERE source_uid=?", (source_uid,)).fetchone()
                matched_by_id = False
                for message_id in ids:
                    matched = db.execute("""SELECT m.id,m.recipient_email FROM messages m JOIN batches b ON b.id=m.batch_id
                                            WHERE m.message_id=? AND b.owner_email=?""", (message_id, owner_email)).fetchone()
                    if matched:
                        matched_by_id = True
                        break
                if not matched and recipient:
                    candidates = db.execute("""SELECT m.id,m.recipient_email FROM messages m JOIN batches b ON b.id=m.batch_id
                                               WHERE b.owner_email=? AND lower(m.recipient_email)=? AND m.submitted_at>=?
                                               ORDER BY m.submitted_at DESC LIMIT 2""",
                                            (owner_email, recipient, iso(utcnow() - dt.timedelta(days=14)))).fetchall()
                    if len(candidates) == 1:
                        matched = candidates[0]
                matched_id = matched["id"] if matched else None
                if matched_id and not already_seen:
                    field = "bounced_at" if kind == "bounce" else "replied_at"
                    db.execute("UPDATE messages SET {}=COALESCE({},?) WHERE id=?".format(field, field),
                               (iso(utcnow()), matched_id))
                    db.execute("INSERT INTO delivery_events(message_id,kind,observed_at,detail) VALUES (?,?,?,?)",
                               (matched_id, kind, iso(utcnow()), "IMAP 检测到{}".format("退信" if kind == "bounce" else "回复")))
                    if (permanent_failure and matched_by_id and recipient and
                            matched["recipient_email"].lower() == recipient):
                        reason = "IMAP 确认永久退信"
                        db.execute("""INSERT OR IGNORE INTO suppressions
                                      (owner_email,recipient_email,reason,created_at) VALUES (?,?,?,?)""",
                                   (owner_email, recipient, reason, iso(utcnow())))
                        db.execute("""UPDATE contacts SET status='invalid',status_reason=?,updated_at=?
                                      WHERE owner_email=? AND email=? AND status='subscribed'""",
                                   (reason, iso(utcnow()), owner_email, recipient))
                        db.execute("""UPDATE messages SET status='suppressed',last_error=?
                                      WHERE lower(recipient_email)=? AND status='queued' AND batch_id IN
                                      (SELECT id FROM batches WHERE owner_email=?)""",
                                   (reason, recipient, owner_email))
                db.execute("INSERT OR IGNORE INTO imap_events(kind,source_uid,matched_message_id,observed_at,detail) VALUES (?,?,?,?,?)",
                           (kind, source_uid, matched_id, iso(utcnow()), recipient or str(msg.get("From", ""))[:200]))
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
        next_birthday = utcnow()
        while not self.stop_event.is_set():
            try:
                dispatch_one(self.path, self.settings)
            except Exception as exc:
                print("发送任务检查失败：{}".format(exc))
            if utcnow() >= next_birthday:
                try:
                    run_birthday_automations(self.path, offset_hours=int(self.settings["UTC_OFFSET_HOURS"]))
                    run_scheduled_automations(self.path, offset_hours=int(self.settings["UTC_OFFSET_HOURS"]))
                except Exception as exc:
                    print("定时草稿检查失败：{}".format(exc))
                next_birthday = utcnow() + dt.timedelta(minutes=5)
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
