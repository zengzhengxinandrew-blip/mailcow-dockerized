"""Standalone Wismail accounts bound to individually verified mailboxes."""

import base64
import hashlib
import hmac
import re
import secrets
import time

from cryptography.fernet import Fernet

from core import connect, iso, utcnow, verify_mailbox_auth


PASSWORD_ROUNDS = 600000
SESSION_SECONDS = 8 * 3600
INVITE_SECONDS = 24 * 3600


def init_accounts(path):
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS local_accounts (
          mailbox_email TEXT PRIMARY KEY,
          password_hash TEXT NOT NULL DEFAULT '',
          invite_hash TEXT,
          invite_expires_at INTEGER,
          active INTEGER NOT NULL DEFAULT 0,
          failed_logins INTEGER NOT NULL DEFAULT 0,
          locked_until INTEGER NOT NULL DEFAULT 0,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          activated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS local_sessions (
          token_hash TEXT PRIMARY KEY,
          mailbox_email TEXT NOT NULL REFERENCES local_accounts(mailbox_email) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_local_sessions_expires ON local_sessions(expires_at);
        """)


def _mailbox(value, admin_email):
    address = value.strip().lower()
    admin_email = admin_email.strip().lower()
    if (len(address) > 254 or not re.fullmatch(r"[^@\s]{1,64}@[a-z0-9][a-z0-9.-]{0,252}", address)
            or address == admin_email or address.rsplit("@", 1)[-1] != admin_email.rsplit("@", 1)[-1]):
        raise ValueError("请输入与管理员同域的其他邮箱地址")
    return address


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_hash(password):
    if len(password) < 12 or len(password) > 256:
        raise ValueError("Wismail 密码需为 12 至 256 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ROUNDS, base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"))


def _check_password(password, encoded):
    try:
        scheme, rounds, salt, digest = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256" or not 1 <= int(rounds) <= PASSWORD_ROUNDS:
            return False
        expected = base64.urlsafe_b64decode(digest.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                    base64.urlsafe_b64decode(salt.encode("ascii")), int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def create_invite(path, mailbox_email, admin_email):
    mailbox_email = _mailbox(mailbox_email, admin_email)
    code = secrets.token_urlsafe(32)
    now = int(time.time())
    with connect(path) as db:
        row = db.execute("SELECT active FROM local_accounts WHERE mailbox_email=?", (mailbox_email,)).fetchone()
        if row and row["active"]:
            raise ValueError("该邮箱已有子账户；如需重设，请使用重置操作")
        if row:
            db.execute("""UPDATE local_accounts SET invite_hash=?,invite_expires_at=?,created_by=?
                          WHERE mailbox_email=?""", (_digest(code), now + INVITE_SECONDS, admin_email, mailbox_email))
        else:
            db.execute("""INSERT INTO local_accounts(mailbox_email,invite_hash,invite_expires_at,created_by,created_at)
                          VALUES (?,?,?,?,?)""", (mailbox_email, _digest(code), now + INVITE_SECONDS,
                                                 admin_email, iso(utcnow())))
    return mailbox_email, code


def reset_invite(path, mailbox_email, admin_email):
    mailbox_email = _mailbox(mailbox_email, admin_email)
    code = secrets.token_urlsafe(32)
    with connect(path) as db:
        changed = db.execute("""UPDATE local_accounts SET active=0,password_hash='',invite_hash=?,
                              invite_expires_at=?,failed_logins=0,locked_until=0
                              WHERE mailbox_email=? AND created_by=?""",
                             (_digest(code), int(time.time()) + INVITE_SECONDS, mailbox_email, admin_email)).rowcount
        if not changed:
            raise ValueError("子账户不存在")
        db.execute("DELETE FROM local_sessions WHERE mailbox_email=?", (mailbox_email,))
        db.execute("UPDATE batches SET state='paused' WHERE owner_email=? AND state='active'", (mailbox_email,))
    return mailbox_email, code


def disable_account(path, mailbox_email, admin_email):
    mailbox_email = _mailbox(mailbox_email, admin_email)
    with connect(path) as db:
        changed = db.execute("""UPDATE local_accounts SET active=0,password_hash='',invite_hash=NULL,
                              invite_expires_at=NULL WHERE mailbox_email=? AND created_by=?""",
                             (mailbox_email, admin_email)).rowcount
        if not changed:
            raise ValueError("子账户不存在")
        db.execute("DELETE FROM local_sessions WHERE mailbox_email=?", (mailbox_email,))
        db.execute("UPDATE batches SET state='paused' WHERE owner_email=? AND state='active'", (mailbox_email,))


def list_accounts(path, admin_email):
    with connect(path) as db:
        return db.execute("""SELECT mailbox_email,active,invite_expires_at,created_at,activated_at
                             FROM local_accounts WHERE created_by=? ORDER BY created_at DESC""",
                          (admin_email,)).fetchall()


def oauth_allowed(path, mailbox_email):
    with connect(path) as db:
        row = db.execute("SELECT active FROM local_accounts WHERE mailbox_email=?",
                         (mailbox_email.strip().lower(),)).fetchone()
        return row is None or bool(row["active"])


def activate_account(path, mailbox_email, code, smtp_password, imap_password, password, settings):
    mailbox_email = mailbox_email.strip().lower()
    now = int(time.time())
    with connect(path) as db:
        row = db.execute("""SELECT invite_hash,invite_expires_at,active FROM local_accounts
                            WHERE mailbox_email=?""", (mailbox_email,)).fetchone()
    if (not row or row["active"] or not row["invite_hash"] or row["invite_expires_at"] <= now
            or not hmac.compare_digest(row["invite_hash"], _digest(code))):
        raise ValueError("激活信息无效或已过期")
    encoded_password = _password_hash(password)
    if not smtp_password or not settings.get("CREDENTIAL_KEY"):
        raise ValueError("缺少邮箱应用密码或服务器加密密钥")
    verify_mailbox_auth(mailbox_email, smtp_password, imap_password, settings)
    cipher = Fernet(settings["CREDENTIAL_KEY"].encode("ascii"))
    smtp_enc = cipher.encrypt(smtp_password.encode("utf-8")).decode("ascii")
    imap_enc = cipher.encrypt(imap_password.encode("utf-8")).decode("ascii") if imap_password else None
    with connect(path) as db:
        changed = db.execute("""UPDATE local_accounts SET password_hash=?,active=1,invite_hash=NULL,
                              invite_expires_at=NULL,activated_at=?,failed_logins=0,locked_until=0
                              WHERE mailbox_email=? AND active=0 AND invite_hash=? AND invite_expires_at>?""",
                             (encoded_password, iso(utcnow()), mailbox_email, _digest(code), now)).rowcount
        if not changed:
            raise ValueError("激活信息无效或已使用")
        db.execute("""INSERT OR REPLACE INTO mailbox_auth(owner_email,smtp_password_enc,imap_password_enc,updated_at)
                      VALUES (?,?,?,?)""", (mailbox_email, smtp_enc, imap_enc, iso(utcnow())))


def login(path, mailbox_email, password):
    mailbox_email = mailbox_email.strip().lower()
    now = int(time.time())
    with connect(path) as db:
        row = db.execute("SELECT password_hash,active,failed_logins,locked_until FROM local_accounts WHERE mailbox_email=?",
                         (mailbox_email,)).fetchone()
        if not row or not row["active"]:
            raise ValueError("账号或密码错误")
        if row["locked_until"] > now:
            raise ValueError("登录尝试过多，请稍后再试")
        if not _check_password(password, row["password_hash"]):
            failures = row["failed_logins"] + 1
            db.execute("UPDATE local_accounts SET failed_logins=?,locked_until=? WHERE mailbox_email=?",
                       (failures, now + 900 if failures >= 5 else 0, mailbox_email))
            db.commit()
            raise ValueError("账号或密码错误")
        db.execute("UPDATE local_accounts SET failed_logins=0,locked_until=0 WHERE mailbox_email=?", (mailbox_email,))
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM local_sessions WHERE expires_at<=?", (now,))
        db.execute("INSERT INTO local_sessions(token_hash,mailbox_email,expires_at) VALUES (?,?,?)",
                   (_digest(token), mailbox_email, now + SESSION_SECONDS))
        return token


def read_session(path, token):
    if not token or len(token) > 128 or "." in token:
        return None
    with connect(path) as db:
        row = db.execute("""SELECT a.mailbox_email FROM local_sessions s JOIN local_accounts a
                            ON a.mailbox_email=s.mailbox_email WHERE s.token_hash=? AND s.expires_at>?
                            AND a.active=1""", (_digest(token), int(time.time()))).fetchone()
        return row["mailbox_email"] if row else None


def revoke_session(path, token):
    if token and len(token) <= 128 and "." not in token:
        with connect(path) as db:
            db.execute("DELETE FROM local_sessions WHERE token_hash=?", (_digest(token),))
