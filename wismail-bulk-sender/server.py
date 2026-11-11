"""Small, self-contained Wismail bulk send UI.

Run one process only. SMTP credentials remain server-side and are never put in uploads.
"""

import base64
import cgi
import csv
import datetime as dt
import hmac
import html
import http.cookies
import io
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

from core import (MAX_UPLOAD_BYTES, Worker, add_suppression, cancel_batch, connect, create_draft,
                  init_db, mailbox_settings, parse_upload, revoke_mailbox_auth, save_mailbox_auth,
                  schedule_batch, set_batch_state, utcnow, verify_mailbox_auth)
import oauth_login
from template_xlsx import build_template


ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("WISMAIL_DB_PATH", os.path.join(ROOT, "data", "wismail.sqlite3"))
URL_PREFIX = os.getenv("WISMAIL_URL_PREFIX", "").rstrip("/")
ADMIN_USER = os.getenv("WISMAIL_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("WISMAIL_ADMIN_PASSWORD", "")
CSRF_TOKEN = secrets.token_urlsafe(32)
SETTINGS = {
    "SMTP_HOST": os.getenv("WISMAIL_SMTP_HOST", ""),
    "SMTP_PORT": os.getenv("WISMAIL_SMTP_PORT", "587"),
    "SMTP_USER": os.getenv("WISMAIL_SMTP_USER", ""),
    "SMTP_PASSWORD": os.getenv("WISMAIL_SMTP_PASSWORD", ""),
    "SMTP_FROM": os.getenv("WISMAIL_SMTP_FROM", ""),
    "IMAP_HOST": os.getenv("WISMAIL_IMAP_HOST", ""),
    "IMAP_PORT": os.getenv("WISMAIL_IMAP_PORT", "993"),
    "IMAP_USER": os.getenv("WISMAIL_IMAP_USER", ""),
    "IMAP_PASSWORD": os.getenv("WISMAIL_IMAP_PASSWORD", ""),
    "DAILY_LIMIT": os.getenv("WISMAIL_DAILY_LIMIT", "100"),
    "MIN_INTERVAL_SECONDS": os.getenv("WISMAIL_MIN_INTERVAL_SECONDS", "60"),
    "UTC_OFFSET_HOURS": os.getenv("WISMAIL_UTC_OFFSET_HOURS", "8"),
    "CREDENTIAL_KEY": os.getenv("WISMAIL_CREDENTIAL_KEY", ""),
    "OAUTH_BASE_URL": os.getenv("WISMAIL_OAUTH_BASE_URL", ""),
    "OAUTH_CLIENT_ID": os.getenv("WISMAIL_OAUTH_CLIENT_ID", ""),
    "OAUTH_CLIENT_SECRET": os.getenv("WISMAIL_OAUTH_CLIENT_SECRET", ""),
    "OAUTH_REDIRECT_URI": os.getenv("WISMAIL_OAUTH_REDIRECT_URI", ""),
    "SESSION_SECRET": os.getenv("WISMAIL_SESSION_SECRET", ""),
}
STATUS = {
    "draft": "待确认", "queued": "待发送", "sending": "发送中",
    "submitted": "已提交发信服务器", "failed": "发送失败",
    "needs_review": "需人工核对", "cancelled": "已取消", "suppressed": "不再联系",
}
STATE = {"draft": "待确认", "active": "发送中", "paused": "已暂停", "completed": "已完成", "cancelled": "已取消"}


def h(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def csv_safe(value):
    """Prevent spreadsheet programs from treating exported text as a formula."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def local_time(utc_string):
    if not utc_string:
        return "—"
    value = dt.datetime.strptime(utc_string[:19], "%Y-%m-%dT%H:%M:%S")
    return (value + dt.timedelta(hours=int(SETTINGS["UTC_OFFSET_HOURS"]))).strftime("%Y-%m-%d %H:%M")


def page(title, content, user=""):
    account_link = ('<span class="top-meta">{}</span><a class="top-account" href="/account">发件授权</a>'
                    '<a class="top-account" href="/logout">退出</a>'.format(h(user)) if oauth_login.configured(SETTINGS) and user
                    else '<span class="top-meta">外贸邮件工作台 / V2</span>')
    markup = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Wismail</title>
<link rel="stylesheet" href="/static.css"></head><body>
<header class="topbar"><div class="shell top-inner"><a class="brand" href="/"><span class="brand-mark">W</span><span>Wismail <small>批量发送</small></span></a><div class="top-actions">{account_link}</div></div></header>
<main class="shell">{content}</main><footer class="shell footer">Wismail · 已提交不代表进入客户收件箱</footer></body></html>'''.format(title=h(title), content=content, account_link=account_link)
    if URL_PREFIX:
        markup = markup.replace('href="/', 'href="' + URL_PREFIX + '/').replace('action="/', 'action="' + URL_PREFIX + '/')
    return markup


def csrf(user="__legacy_admin__"):
    token = oauth_login.csrf_token(user, SETTINGS) if oauth_login.configured(SETTINGS) else CSRF_TOKEN
    return '<input type="hidden" name="csrf" value="{}">'.format(token)


def summary_for(db, batch_id):
    rows = db.execute("SELECT status,submitted_at,replied_at,bounced_at FROM messages WHERE batch_id=?", (batch_id,)).fetchall()
    return {
        "total": len(rows),
        "queued": sum(x["status"] == "queued" for x in rows),
        "submitted": sum(bool(x["submitted_at"]) for x in rows),
        "failed": sum(x["status"] in ("failed", "needs_review") for x in rows),
        "suppressed": sum(x["status"] == "suppressed" for x in rows),
        "replied": sum(bool(x["replied_at"]) for x in rows),
        "bounced": sum(bool(x["bounced_at"]) for x in rows),
    }


def dashboard(error="", user="__legacy_admin__"):
    with connect(DB_PATH) as db:
        batches = db.execute("SELECT * FROM batches WHERE owner_email=? ORDER BY id DESC LIMIT 50", (user,)).fetchall()
        suppressions = db.execute("""SELECT recipient_email,created_at FROM suppressions
                                   WHERE owner_email IN (?, '*') ORDER BY created_at DESC LIMIT 10""", (user,)).fetchall()
        suppression_count = db.execute("SELECT COUNT(*) AS n FROM suppressions WHERE owner_email IN (?, '*')", (user,)).fetchone()["n"]
        cards = []
        for batch in batches:
            stats = summary_for(db, batch["id"])
            cards.append('''<a class="batch-row" href="/batches/{id}"><div><span class="batch-name">{name}</span><span class="muted">创建于 {created}</span></div><div class="batch-facts"><span class="pill {state}">{state_label}</span><span>{total} 封</span><span>已提交 {submitted}</span><span>失败 {failed}</span></div></a>'''.format(
                id=batch["id"], name=h(batch["name"]), created=local_time(batch["created_at"]),
                state=h(batch["state"]), state_label=STATE.get(batch["state"], batch["state"]), **stats))
    alert = '<div class="alert">{}</div>'.format(h(error).replace("\n", "<br>")) if error else ""
    account = mailbox_settings(DB_PATH, user, SETTINGS)
    ready = bool(account and all(account.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")))
    status = '<span class="ready">● 发件账号已配置：{}</span>'.format(h(account["SMTP_FROM"])) if ready else '<span class="not-ready">● 尚未配置发件账号；可先导入预览</span>'
    content = '''<section class="hero"><div class="eyebrow">一行一封 · 无 AI 调用</div><h1>把写好的邮件，<br><em>按计划发出去。</em></h1><p>上传 AI Agent 在外部整理的完整邮件。Wismail 检查格式、排队发送并记录每封邮件的结果。</p></section>
{alert}<div class="two-col"><section class="panel"><div class="panel-title"><span class="step">01</span><h2>上传邮件表格</h2></div><p class="muted">每行填写收件邮箱、邮件主题、邮件正文。支持 .xlsx 和 UTF-8 .csv，单批最多 1000 封。</p>
<a class="text-link" href="/template.xlsx">下载 Excel 模板 ↗</a>
<form method="post" action="/upload" enctype="multipart/form-data">{csrf}
<label for="name">批次名称</label><input id="name" name="name" maxlength="120" placeholder="例如：11 月德国客户报价跟进" required>
<label for="file">已填好的邮件文件</label><input id="file" name="file" type="file" accept=".xlsx,.csv" required>
<button class="button" type="submit">上传并检查</button></form></section>
<section class="panel side"><div class="panel-title"><span class="step">02</span><h2>检查后再发送</h2></div><ol class="process"><li>检查邮箱、主题、正文和重复邮件</li><li>逐封预览，设置开始时间与发送间隔</li><li>启动后查看提交、失败、退信和回复</li></ol><div class="config">{status}<small>默认每日上限 {limit} 封，最短发送间隔 {interval} 秒。退信和回复需要配置 IMAP。</small></div></section></div>
<section class="list-section"><div class="section-heading"><h2>发送批次</h2><span class="muted">最近 50 个</span></div><div class="batch-list">{batches}</div></section>
<section class="panel suppression"><div><div class="eyebrow">发送保护</div><h2>不再联系名单 <small>共 {suppression_count} 个</small></h2><p class="muted">客户要求停止联系时，先将邮箱加入这里。已排队但未发送的邮件也会被跳过。</p></div>
<div><form method="post" action="/suppressions">{csrf}<label for="suppression-email">添加邮箱</label><div class="inline-form"><input id="suppression-email" type="email" name="email" placeholder="buyer@example.com" required><button class="button" type="submit">加入名单</button></div></form>
<div class="suppression-list">{suppressions}</div></div></section>'''.format(
        alert=alert, csrf=csrf(user), status=status, limit=h(SETTINGS["DAILY_LIMIT"]),
        interval=h(SETTINGS["MIN_INTERVAL_SECONDS"]),
        batches="".join(cards) if cards else '<div class="empty">还没有发送批次。下载模板，填好第一批邮件后上传。</div>',
        suppression_count=suppression_count,
        suppressions="".join('<div><span>{}</span><small>{}</small></div>'.format(h(x["recipient_email"]), local_time(x["created_at"])) for x in suppressions) if suppressions else '<p class="muted">名单为空</p>')
    return page("发送批次", content, user)


def batch_page(batch_id, error="", user="__legacy_admin__"):
    with connect(DB_PATH) as db:
        batch = db.execute("SELECT * FROM batches WHERE id=? AND owner_email=?", (batch_id, user)).fetchone()
        if not batch:
            return None
        rows = db.execute("SELECT * FROM messages WHERE batch_id=? ORDER BY row_number", (batch_id,)).fetchall()
        stats = summary_for(db, batch_id)
    alert = '<div class="alert">{}</div>'.format(h(error).replace("\n", "<br>")) if error else ""
    offset = int(SETTINGS["UTC_OFFSET_HOURS"])
    default_time = (utcnow() + dt.timedelta(hours=offset, minutes=5)).strftime("%Y-%m-%dT%H:%M")
    controls = ""
    if batch["state"] == "draft":
        controls = '''<section class="panel launch"><div><div class="eyebrow">检查完成</div><h2>设置发送计划</h2><p class="muted">启动前请抽查正文、报价与收件人。开始后按顺序逐封发送。</p></div>
<form method="post" action="/batches/{id}/start">{csrf}<div class="form-grid"><div><label for="start">开始时间（UTC{offset:+d}）</label><input type="datetime-local" id="start" name="start" value="{default}" required></div>
<div><label for="interval">每封间隔（秒）</label><input type="number" id="interval" name="interval" min="{minimum}" max="86400" value="120" required></div></div>
<button class="button" type="submit">确认并开始排队</button></form></section>'''.format(
            id=batch_id, csrf=csrf(user), offset=offset, default=default_time, minimum=h(SETTINGS["MIN_INTERVAL_SECONDS"]))
    elif batch["state"] in ("active", "paused"):
        action = "pause" if batch["state"] == "active" else "resume"
        label = "暂停剩余邮件" if action == "pause" else "继续发送"
        controls = '''<div class="actions"><form method="post" action="/batches/{id}/{action}">{csrf}<button class="button secondary">{label}</button></form>
<form method="post" action="/batches/{id}/cancel">{csrf}<button class="button subtle" onclick="return confirm('取消后，待发送邮件不会再发出。确定取消？')">取消剩余邮件</button></form></div>'''.format(
            id=batch_id, action=action, csrf=csrf(user), label=label)
    stat_html = "".join('<div class="stat"><strong>{}</strong><span>{}</span></div>'.format(stats[key], label) for key, label in
                        (("total", "总邮件"), ("queued", "待发送"), ("submitted", "已提交"),
                         ("failed", "失败或待核对"), ("suppressed", "不再联系"),
                         ("bounced", "检测到退信"), ("replied", "检测到回复")))
    row_html = []
    for row in rows:
        status = STATUS.get(row["status"], row["status"])
        flags = []
        if row["bounced_at"]:
            flags.append('<span class="pill failed">退信</span>')
        if row["replied_at"]:
            flags.append('<span class="pill replied">已回复</span>')
        row_html.append('''<tr><td class="row-no">{no}</td><td><strong>{email}</strong><div class="muted">{subject}</div><details><summary>查看正文</summary><pre>{body}</pre></details></td>
<td><span class="pill {raw_status}">{status}</span> {flags}</td><td>{scheduled}</td><td>{submitted}</td><td class="error-text">{error}</td></tr>'''.format(
            no=row["row_number"], email=h(row["recipient_email"]), subject=h(row["subject"]),
            body=h(row["body"]), raw_status=h(row["status"]), status=h(status), flags=" ".join(flags),
            scheduled=local_time(row["scheduled_at"]), submitted=local_time(row["submitted_at"]),
            error=h(row["last_error"] or "—")))
    account = mailbox_settings(DB_PATH, user, SETTINGS)
    content = '''<nav class="back"><a href="/">← 返回批次列表</a></nav><section class="batch-head"><div><div class="eyebrow">批次 #{id} · {state}</div><h1>{name}</h1><p class="muted">创建于 {created} · 发件账号 {sender}</p></div><a class="text-link" href="/batches/{id}/export.csv">导出明细 CSV ↗</a></section>
{alert}<section class="stats">{stats}</section>{controls}<section class="list-section"><div class="section-heading"><h2>逐封结果</h2><span class="muted">已提交表示发信服务器已接受，不代表进入收件箱。</span></div>
<div class="table-wrap"><table><thead><tr><th>行</th><th>邮件</th><th>状态</th><th>计划时间</th><th>提交时间</th><th>原因</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''.format(
        id=batch_id, state=STATE.get(batch["state"], batch["state"]), name=h(batch["name"]),
        created=local_time(batch["created_at"]), sender=h(account["SMTP_FROM"] if account else "未配置"),
        alert=alert, stats=stat_html, controls=controls, rows="".join(row_html))
    return page(batch["name"], content, user)


def account_page(user, error="", success=False):
    with connect(DB_PATH) as db:
        row = db.execute("SELECT updated_at,imap_password_enc FROM mailbox_auth WHERE owner_email=?", (user,)).fetchone()
    alert = '<div class="alert">{}</div>'.format(h(error)) if error else ""
    confirmation = '<div class="notice">发件授权已验证并保存。现在可以返回首页启动批次。</div>' if success else ""
    state = ('已配置 · 上次更新 ' + local_time(row["updated_at"])) if row else "尚未配置"
    imap_state = "已开启" if row and row["imap_password_enc"] else "未开启"
    content = '''<nav class="back"><a href="/">← 返回发送批次</a></nav><section class="batch-head"><div><div class="eyebrow">邮箱授权</div><h1>让后台以你的邮箱发信</h1><p class="muted">当前登录：{user}。发件人固定为这个邮箱地址，不能代其他账号发送。</p></div></section>
{alert}{confirmation}<div class="two-col"><section class="panel"><h2>配置应用密码</h2><p class="muted">请输入在邮箱设置中创建的应用密码。Wismail 会先测试登录，不会发送测试邮件；通过后加密保存，供定时任务使用。</p>
<form method="post" action="/account">{csrf}<label for="smtp-password">SMTP 应用密码</label><input id="smtp-password" name="smtp_password" type="password" autocomplete="new-password" required>
<label for="imap-password">IMAP 应用密码（可选）</label><input id="imap-password" name="imap_password" type="password" autocomplete="new-password">
<p class="muted">配置 IMAP 后才能尝试统计回复和退信。部分邮箱可使用同一应用密码。</p><button class="button" type="submit">测试并保存授权</button></form></section>
<section class="panel side"><h2>当前状态</h2><div class="account-state"><div><span>SMTP 发件授权</span><strong>{state}</strong></div><div><span>IMAP 状态同步</span><strong>{imap_state}</strong></div><div><span>SMTP 服务器</span><strong>{smtp_host}:{smtp_port}</strong></div><div><span>IMAP 服务器</span><strong>{imap_host}:{imap_port}</strong></div></div>
<p class="muted">登录 Wismail 的密码只用于网页登录。后台定时发送需要独立的应用密码；该密码不会显示在页面或上传表格中。</p>
<form method="post" action="/account/revoke">{csrf}<button class="button subtle" type="submit" onclick="return confirm('撤销授权后，正在排队的批次会暂停。确定撤销？')">撤销发件授权</button></form></section></div>'''.format(
        user=h(user), alert=alert, confirmation=confirmation, csrf=csrf(user), state=h(state),
        imap_state=h(imap_state), smtp_host=h(SETTINGS["SMTP_HOST"] or "未配置"),
        smtp_port=h(SETTINGS["SMTP_PORT"]), imap_host=h(SETTINGS["IMAP_HOST"] or "未配置"),
        imap_port=h(SETTINGS["IMAP_PORT"]))
    return page("发件授权", content, user)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        # OAuth callback URLs contain short-lived codes; never write them to access logs.
        return

    def _authorized(self):
        value = self.headers.get("Authorization", "")
        if not value.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(value[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(supplied, ADMIN_USER + ":" + ADMIN_PASSWORD)

    def _send(self, code, body, content_type="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _auth_or_401(self):
        if oauth_login.configured(SETTINGS):
            cookies = http.cookies.SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
                token = cookies["wismail_session"].value if "wismail_session" in cookies else ""
            except http.cookies.CookieError:
                token = ""
            self.current_user = oauth_login.read_session(token, SETTINGS)
            if self.current_user:
                return True
            self._redirect("/login")
            return False
        if self._authorized():
            self.current_user = "__legacy_admin__"
            return True
        self._send(401, "需要登录", extra={"WWW-Authenticate": 'Basic realm="Wismail bulk sender"'})
        return False

    def _redirect(self, destination, cookie=None):
        if URL_PREFIX and destination.startswith("/") and not destination.startswith(URL_PREFIX + "/"):
            destination = URL_PREFIX + destination
        headers = {"Location": destination}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(303, b"", extra=headers)

    def _secure_cookie(self):
        return "; Secure" if SETTINGS["OAUTH_REDIRECT_URI"].startswith("https://") else ""

    def _route_path(self):
        raw_path = urlparse(self.path).path
        if not URL_PREFIX:
            return raw_path
        if raw_path == URL_PREFIX:
            return "/"
        if raw_path.startswith(URL_PREFIX + "/"):
            return raw_path[len(URL_PREFIX):]
        return None

    def do_GET(self):
        path = self._route_path()
        if path is None:
            self._send(404, "页面不存在")
            return
        if oauth_login.configured(SETTINGS) and path == "/login":
            state, cookie = oauth_login.new_state(SETTINGS)
            header = "wismail_oauth_state={}; Path={}/oauth/callback; Max-Age=600; HttpOnly; SameSite=Lax{}".format(
                cookie, URL_PREFIX, self._secure_cookie())
            self._redirect(oauth_login.authorize_url(SETTINGS, state), cookie=header)
            return
        if oauth_login.configured(SETTINGS) and path == "/oauth/callback":
            query = parse_qs(urlparse(self.path).query)
            cookies = http.cookies.SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
                saved_state = cookies["wismail_oauth_state"].value if "wismail_oauth_state" in cookies else ""
            except http.cookies.CookieError:
                saved_state = ""
            state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            if not code or not oauth_login.valid_state(state, saved_state, SETTINGS):
                self._send(400, page("登录失败", '<div class="alert">登录请求已失效，请返回重新登录。</div>'))
                return
            try:
                user = oauth_login.exchange_code(SETTINGS, code)
            except Exception:
                self._send(400, page("登录失败", '<div class="alert">邮箱授权失败，请检查 mailcow OAuth2 配置后重试。</div>'))
                return
            session = oauth_login.new_session(user, SETTINGS)
            header = "wismail_session={}; Path={}/; Max-Age=28800; HttpOnly; SameSite=Lax{}".format(
                session, URL_PREFIX, self._secure_cookie())
            self._redirect("/", cookie=header)
            return
        if oauth_login.configured(SETTINGS) and path == "/logout":
            self._redirect("/login", cookie="wismail_session=; Path={}/; Max-Age=0; HttpOnly; SameSite=Lax{}".format(URL_PREFIX, self._secure_cookie()))
            return
        if not self._auth_or_401():
            return
        if path == "/":
            self._send(200, dashboard(user=self.current_user))
        elif path == "/account" and oauth_login.configured(SETTINGS):
            self._send(200, account_page(self.current_user))
        elif path == "/static.css":
            with open(os.path.join(ROOT, "static.css"), "rb") as stream:
                self._send(200, stream.read(), "text/css; charset=utf-8")
        elif path == "/template.xlsx":
            self._send(200, build_template(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       {"Content-Disposition": 'attachment; filename="wismail-upload-template.xlsx"'})
        else:
            match = re.match(r"^/batches/(\d+)(/export\.csv)?$", path)
            if not match:
                self._send(404, page("未找到", '<div class="empty">页面不存在。<a href="/">返回首页</a></div>', self.current_user))
                return
            batch_id = int(match.group(1))
            if match.group(2):
                with connect(DB_PATH) as db:
                    batch = db.execute("SELECT id FROM batches WHERE id=? AND owner_email=?", (batch_id, self.current_user)).fetchone()
                    rows = db.execute("SELECT row_number,recipient_email,subject,status,scheduled_at,submitted_at,bounced_at,replied_at,last_error FROM messages WHERE batch_id=? ORDER BY row_number", (batch_id,)).fetchall() if batch else None
                if rows is None:
                    self._send(404, "批次不存在")
                    return
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["表格行号", "收件邮箱", "邮件主题", "状态", "计划时间(UTC)", "提交时间(UTC)", "退信时间(UTC)", "回复时间(UTC)", "原因"])
                for row in rows:
                    writer.writerow([csv_safe(value) for value in row])
                self._send(200, b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="wismail-batch-{}.csv"'.format(batch_id)})
            else:
                rendered = batch_page(batch_id, user=self.current_user)
                self._send(200 if rendered else 404, rendered or "批次不存在")

    def _form(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES + 1024 * 1024:
            raise ValueError("上传请求过大或为空")
        return cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
                                keep_blank_values=True)

    def do_POST(self):
        if not self._auth_or_401():
            return
        path = self._route_path()
        if path is None:
            self._send(404, "操作不存在")
            return
        try:
            form = self._form()
            expected_csrf = (oauth_login.csrf_token(self.current_user, SETTINGS)
                             if oauth_login.configured(SETTINGS) else CSRF_TOKEN)
            if not hmac.compare_digest(form.getfirst("csrf", ""), expected_csrf):
                self._send(403, "请求校验失败，请刷新页面后重试")
                return
            if path == "/account" and oauth_login.configured(SETTINGS):
                smtp_password = form.getfirst("smtp_password", "")
                imap_password = form.getfirst("imap_password", "")
                if not SETTINGS["CREDENTIAL_KEY"]:
                    raise ValueError("服务器尚未配置凭证加密密钥")
                try:
                    verify_mailbox_auth(self.current_user, smtp_password, imap_password, SETTINGS)
                except Exception:
                    raise ValueError("邮箱授权验证失败，请检查服务器地址和应用密码")
                save_mailbox_auth(DB_PATH, self.current_user, smtp_password, imap_password, SETTINGS["CREDENTIAL_KEY"])
                self._send(200, account_page(self.current_user, success=True))
                return
            if path == "/account/revoke" and oauth_login.configured(SETTINGS):
                revoke_mailbox_auth(DB_PATH, self.current_user)
                self._redirect("/account")
                return
            if path == "/suppressions":
                add_suppression(DB_PATH, form.getfirst("email", ""), owner_email=self.current_user)
                self._redirect("/")
                return
            if path == "/upload":
                item = form["file"] if "file" in form else None
                if item is None or not getattr(item, "filename", None):
                    raise ValueError("请选择 Excel 或 CSV 文件")
                data = item.file.read(MAX_UPLOAD_BYTES + 1)
                messages = parse_upload(item.filename, data)
                batch_id = create_draft(DB_PATH, form.getfirst("name", ""), messages, self.current_user)
                self._redirect("/batches/{}".format(batch_id))
                return
            match = re.match(r"^/batches/(\d+)/(start|pause|resume|cancel)$", path)
            if not match:
                self._send(404, "操作不存在")
                return
            batch_id, action = int(match.group(1)), match.group(2)
            with connect(DB_PATH) as db:
                owned = db.execute("SELECT 1 FROM batches WHERE id=? AND owner_email=?", (batch_id, self.current_user)).fetchone()
            if not owned:
                self._send(404, "批次不存在")
                return
            if action == "start":
                account = mailbox_settings(DB_PATH, self.current_user, SETTINGS)
                if not account or not all(account.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")):
                    raise ValueError("请先配置此邮箱的 SMTP 发件授权")
                interval = int(form.getfirst("interval", "0"))
                if interval < int(SETTINGS["MIN_INTERVAL_SECONDS"]):
                    raise ValueError("发送间隔小于服务器允许的最短间隔")
                schedule_batch(DB_PATH, batch_id, form.getfirst("start", ""), interval, int(SETTINGS["UTC_OFFSET_HOURS"]))
            elif action == "pause":
                set_batch_state(DB_PATH, batch_id, "paused")
            elif action == "resume":
                set_batch_state(DB_PATH, batch_id, "active")
            else:
                cancel_batch(DB_PATH, batch_id)
            self._redirect("/batches/{}".format(batch_id))
        except (ValueError, KeyError) as exc:
            if path == "/account" and oauth_login.configured(SETTINGS):
                self._send(400, account_page(self.current_user, str(exc)))
                return
            match = re.match(r"^/batches/(\d+)/", path)
            rendered = batch_page(int(match.group(1)), str(exc), self.current_user) if match else dashboard(str(exc), self.current_user)
            self._send(400, rendered or dashboard(str(exc), self.current_user))


def main():
    if URL_PREFIX and (not URL_PREFIX.startswith("/") or "//" in URL_PREFIX or "?" in URL_PREFIX):
        raise SystemExit("WISMAIL_URL_PREFIX 必须是以 / 开头的路径，例如 /bulk")
    oauth_keys = ("OAUTH_BASE_URL", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_REDIRECT_URI", "SESSION_SECRET")
    if any(SETTINGS.get(key) for key in oauth_keys):
        if not oauth_login.configured(SETTINGS):
            raise SystemExit("mailcow OAuth2 配置不完整")
        if not SETTINGS["OAUTH_BASE_URL"].startswith("https://") or not SETTINGS["OAUTH_REDIRECT_URI"].startswith("https://"):
            raise SystemExit("OAuth2 地址必须使用 HTTPS")
        if urlparse(SETTINGS["OAUTH_REDIRECT_URI"]).path != URL_PREFIX + "/oauth/callback" or len(SETTINGS["SESSION_SECRET"]) < 32:
            raise SystemExit("OAuth2 回调地址或会话密钥无效")
        if not SETTINGS["CREDENTIAL_KEY"]:
            raise SystemExit("请设置 WISMAIL_CREDENTIAL_KEY")
        try:
            Fernet(SETTINGS["CREDENTIAL_KEY"].encode("ascii"))
        except Exception:
            raise SystemExit("WISMAIL_CREDENTIAL_KEY 格式无效；请使用 Fernet.generate_key() 生成")
    elif not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12:
        raise SystemExit("请设置至少 12 位的 WISMAIL_ADMIN_PASSWORD")
    if int(SETTINGS["DAILY_LIMIT"]) < 1 or int(SETTINGS["MIN_INTERVAL_SECONDS"]) < 1:
        raise SystemExit("发信上限和发送间隔必须为正数")
    init_db(DB_PATH)
    worker = Worker(DB_PATH, SETTINGS)
    worker.start()
    host = os.getenv("WISMAIL_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("WISMAIL_PORT", "8765"))
    print("Wismail 批量发送模块运行在 http://{}:{}".format(host, port))
    try:
        HTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop_event.set()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
