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
import json
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

from core import (MAX_UPLOAD_BYTES, Worker, ab_click_guidance, add_suppression, cancel_batch, connect, copy_batch, copy_template, create_draft,
                  create_contact_campaign, get_template, import_contacts, init_db, mailbox_settings,
                  parse_contacts_upload, parse_upload, prepare_contact_campaign, render_template, report_data,
                  save_template, send_template_test,
                  revoke_mailbox_auth, save_contact, save_mailbox_auth, schedule_batch,
                  set_batch_state, set_contact_deleted, set_contact_status, tracking_ready,
                  record_tracking_event, verify_tracking_token, utcnow, verify_mailbox_auth)
from core import unsubscribe_token, verify_unsubscribe_token
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
    "HOURLY_LIMIT": os.getenv("WISMAIL_HOURLY_LIMIT", "20"),
    "MIN_INTERVAL_SECONDS": os.getenv("WISMAIL_MIN_INTERVAL_SECONDS", "60"),
    "UTC_OFFSET_HOURS": os.getenv("WISMAIL_UTC_OFFSET_HOURS", "8"),
    "CREDENTIAL_KEY": os.getenv("WISMAIL_CREDENTIAL_KEY", ""),
    "OAUTH_BASE_URL": os.getenv("WISMAIL_OAUTH_BASE_URL", ""),
    "OAUTH_CLIENT_ID": os.getenv("WISMAIL_OAUTH_CLIENT_ID", ""),
    "OAUTH_CLIENT_SECRET": os.getenv("WISMAIL_OAUTH_CLIENT_SECRET", ""),
    "OAUTH_REDIRECT_URI": os.getenv("WISMAIL_OAUTH_REDIRECT_URI", ""),
    "SESSION_SECRET": os.getenv("WISMAIL_SESSION_SECRET", ""),
    "URL_PREFIX": URL_PREFIX,
}
STATUS = {
    "draft": "待确认", "queued": "待发送", "sending": "发送中",
    "submitted": "已提交发信服务器", "failed": "发送失败",
    "needs_review": "需人工核对", "cancelled": "已取消", "suppressed": "不再联系",
}
STATE = {"draft": "待确认", "active": "发送中", "paused": "已暂停", "completed": "已完成", "cancelled": "已取消"}
TRACKING_PIXEL = base64.b64decode("R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=")


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
    account_link = '<a class="top-account" href="/contacts">联系人</a><a class="top-account" href="/templates">邮件模板</a><a class="top-account" href="/campaigns">创建任务</a><a class="top-account" href="/reports">统计报表</a>' + account_link
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
    rows = db.execute("SELECT status,submitted_at,opened_at,clicked_at,replied_at,bounced_at FROM messages WHERE batch_id=?", (batch_id,)).fetchall()
    return {
        "total": len(rows),
        "queued": sum(x["status"] == "queued" for x in rows),
        "submitted": sum(bool(x["submitted_at"]) for x in rows),
        "opened": sum(bool(x["opened_at"]) for x in rows),
        "clicked": sum(bool(x["clicked_at"]) for x in rows),
        "failed": sum(x["status"] in ("failed", "needs_review") for x in rows),
        "suppressed": sum(x["status"] == "suppressed" for x in rows),
        "replied": sum(bool(x["replied_at"]) for x in rows),
        "bounced": sum(bool(x["bounced_at"]) for x in rows),
    }


def report_period(start, end):
    today = (utcnow() + dt.timedelta(hours=int(SETTINGS["UTC_OFFSET_HOURS"]))).date()
    if not start and not end:
        return today - dt.timedelta(days=29), today
    try:
        first = dt.datetime.strptime(start, "%Y-%m-%d").date()
        last = dt.datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("请填写完整且有效的起止日期")
    if first > last or (last - first).days >= 366:
        raise ValueError("统计时间范围最多为 366 天")
    return first, last


def reports_page(user, start="", end=""):
    first, last = report_period(start, end)
    data = report_data(DB_PATH, user, first, last, int(SETTINGS["UTC_OFFSET_HOURS"]))
    labels = (("submitted", "SMTP 已接受"), ("opened", "观测到打开"), ("clicked", "观测到点击"),
              ("failed", "发送失败/待核对"), ("bounced", "检测到退信"), ("replied", "检测到回复"))
    summary = ''.join('<div class="stat"><strong>{}</strong><span>{}</span></div>'.format(data["totals"][key], label)
                      for key, label in labels)
    def cells(row):
        return ''.join('<td>{}</td>'.format(row[key]) for key, _ in labels)
    day_rows = ''.join('<tr><td>{}</td>{}</tr>'.format(h(day), cells(row)) for day, row in data["days"])
    month_rows = ''.join('<tr><td>{}</td>{}</tr>'.format(h(month), cells(row)) for month, row in data["months"])
    batch_rows = ''.join('<tr><td><a class="text-link" href="/batches/{}">{}</a></td>{}</tr>'.format(
        row["id"], h(row["name"]), cells(row)) for row in data["batches"])
    headings = '<tr><th>维度</th>' + ''.join('<th>{}</th>'.format(label) for _, label in labels) + '</tr>'
    query = 'start={}&amp;end={}'.format(first.isoformat(), last.isoformat())
    content = '''<nav class="back"><a href="/">← 返回批次</a></nav><section class="panel"><h1>统计报表</h1>
<p>以下数字按所选时区内的事件发生日期统计。SMTP 已接受只代表发信服务器收件；退信和回复依赖 IMAP 检测。打开与点击是带签名链接的首次请求，可能来自图片代理或安全扫描，不代表真人阅读。失败是当前仍处于失败或待核对状态、且最后一次尝试落在所选日期内的邮件。</p>
<form method="get" action="/reports" class="report-filter"><label>开始日期<input type="date" name="start" value="{start}" required></label><label>结束日期<input type="date" name="end" value="{end}" required></label><button class="button" type="submit">查看报表</button></form>
<p><a class="text-link" href="/reports/export.csv?{query}">导出报表 CSV ↗</a> <button type="button" class="button secondary print-button" onclick="window.print()">打印或保存 PDF</button></p></section>
<section class="stats report-stats">{summary}</section>
<section class="panel report-table"><h2>按日期</h2><div class="table-wrap"><table><thead>{headings}</thead><tbody>{days}</tbody></table></div></section>
<section class="panel report-table"><h2>按月份</h2><div class="table-wrap"><table><thead>{headings}</thead><tbody>{months}</tbody></table></div></section>
<section class="panel report-table"><h2>按批次</h2><div class="table-wrap"><table><thead>{headings}</thead><tbody>{batches}</tbody></table></div></section>'''.format(
        start=first.isoformat(), end=last.isoformat(), query=query, summary=summary, headings=headings,
        days=day_rows or '<tr><td colspan="7">暂无记录</td></tr>',
        months=month_rows or '<tr><td colspan="7">暂无记录</td></tr>',
        batches=batch_rows or '<tr><td colspan="7">暂无记录</td></tr>')
    return page("统计报表", content, user)


EVENT_LABELS = {"attempt": "提交尝试", "submitted": "SMTP 已接受", "open": "观测到打开", "click": "观测到点击", "retry": "等待重试",
                "failed": "发送失败", "needs_review": "需人工核对", "bounce": "检测到退信", "reply": "检测到回复"}


def event_suggestion(kind, code):
    if kind == "needs_review":
        return "先核对发件箱及收件状态，避免重复发送。"
    if kind == "retry":
        return "系统将按计划重试；请留意后续记录。"
    if kind == "failed":
        if code == 535:
            return "检查发件授权或应用密码。"
        if code == 550:
            return "核对收件地址和服务器拒收原因。"
        return "查看 SMTP 返回信息，修正后再创建新批次。"
    return ""


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
<section class="panel side"><div class="panel-title"><span class="step">02</span><h2>检查后再发送</h2></div><ol class="process"><li>检查邮箱、主题、正文和重复邮件</li><li>逐封预览，设置开始时间与发送间隔</li><li>启动后查看提交、失败、退信和回复</li></ol><div class="config">{status}<small>每日上限 {limit} 封，每小时上限 {hourly} 封，最短发送间隔 {interval} 秒。退信和回复需要配置 IMAP。</small></div></section></div>
<section class="list-section"><div class="section-heading"><h2>发送批次</h2><span class="muted">最近 50 个</span></div><div class="batch-list">{batches}</div></section>
<section class="panel suppression"><div><div class="eyebrow">发送保护</div><h2>不再联系名单 <small>共 {suppression_count} 个</small></h2><p class="muted">客户要求停止联系时，先将邮箱加入这里。已排队但未发送的邮件也会被跳过。</p></div>
<div><form method="post" action="/suppressions">{csrf}<label for="suppression-email">添加邮箱</label><div class="inline-form"><input id="suppression-email" type="email" name="email" placeholder="buyer@example.com" required><button class="button" type="submit">加入名单</button></div></form>
<div class="suppression-list">{suppressions}</div></div></section>'''.format(
        alert=alert, csrf=csrf(user), status=status, limit=h(SETTINGS["DAILY_LIMIT"]),
        interval=h(SETTINGS["MIN_INTERVAL_SECONDS"]), hourly=h(SETTINGS["HOURLY_LIMIT"]),
        batches="".join(cards) if cards else '<div class="empty">还没有发送批次。下载模板，填好第一批邮件后上传。</div>',
        suppression_count=suppression_count,
        suppressions="".join('<div><span>{}</span><small>{}</small></div>'.format(h(x["recipient_email"]), local_time(x["created_at"])) for x in suppressions) if suppressions else '<p class="muted">名单为空</p>')
    return page("发送批次", content, user)


def contacts_page(user, error="", search="", status="", category="", recycle=False):
    query = "SELECT c.*, (SELECT group_concat(g.name, ', ') FROM contact_category_links l JOIN contact_categories g ON g.id=l.category_id WHERE l.contact_id=c.id) AS categories FROM contacts c WHERE c.owner_email=? AND c.deleted_at IS {}".format("NOT NULL" if recycle else "NULL")
    params = [user]
    if search:
        query += " AND (c.email LIKE ? OR c.name LIKE ?)"
        params += ["%" + search[:100] + "%"] * 2
    if status:
        query += " AND c.status=?"
        params.append(status)
    if category:
        query += " AND EXISTS (SELECT 1 FROM contact_category_links l JOIN contact_categories g ON g.id=l.category_id WHERE l.contact_id=c.id AND g.owner_email=? AND g.name=?)"
        params += [user, category]
    with connect(DB_PATH) as db:
        rows = db.execute(query + " ORDER BY c.id DESC LIMIT 200", params).fetchall()
        categories = db.execute("SELECT name FROM contact_categories WHERE owner_email=? ORDER BY name", (user,)).fetchall()
        count = db.execute("SELECT COUNT(*) AS n FROM contacts WHERE owner_email=?", (user,)).fetchone()["n"]
    options = ''.join('<option value="{0}" {1}>{0}</option>'.format(h(x["name"]), 'selected' if x["name"] == category else '') for x in categories)
    items = []
    for row in rows:
        actions = ('<form method="post" action="/contacts/restore">{csrf}<input type="hidden" name="id" value="{id}"><button type="submit">恢复</button></form>' if recycle else
                   '<form method="post" action="/contacts/status">{csrf}<input type="hidden" name="id" value="{id}"><select name="status"><option value="unsubscribed">退订</option><option value="complained">投诉</option><option value="invalid">无效地址</option></select><button type="submit">停止发送</button></form><form method="post" action="/contacts/delete">{csrf}<input type="hidden" name="id" value="{id}"><button type="submit">回收</button></form>')
        items.append('<tr><td>{email}</td><td>{name}</td><td>{categories}</td><td>{status}</td><td>{actions}</td></tr>'.format(
            email=h(row["email"]), name=h(row["name"]), categories=h(row["categories"] or ""),
            status=h(row["status"]), actions=actions.format(csrf=csrf(user), id=row["id"])))
    alert = '<div class="alert">{}</div>'.format(h(error)) if error else ''
    content = '''<nav class="back"><a href="/">← 返回批次</a></nav><section class="panel"><h1>联系人 <small>共 {count} 人</small></h1>{alert}
<p class="muted">导入同一邮箱会合并资料；退订、投诉、无效地址和回收站状态不会被导入重置。发送前再次检查状态。</p>
<div class="two-col"><form method="post" action="/contacts/add">{csrf}<h2>添加联系人</h2><label>邮箱</label><input type="email" name="email" required><label>姓名</label><input name="name"><label>分类（逗号分隔）</label><input name="categories"><button class="button" type="submit">保存</button></form>
<form method="post" action="/contacts/import" enctype="multipart/form-data">{csrf}<h2>导入联系人</h2><p>CSV 或 XLSX：邮箱、姓名、性别、生日、手机、地区、分类、变量1至变量10。</p><input type="file" name="file" accept=".csv,.xlsx" required><button class="button" type="submit">导入并合并</button></form></div></section>
<section class="panel"><form method="get" action="/contacts"><input name="q" value="{search}" placeholder="搜索邮箱或姓名"><select name="status"><option value="">全部状态</option><option value="subscribed" {subscribed}>可发送</option><option value="unsubscribed" {unsubscribed}>退订</option><option value="complained" {complained}>投诉</option><option value="invalid" {invalid}>无效</option></select><select name="category"><option value="">全部分类</option>{options}</select><button type="submit">筛选</button></form>
<p><a href="/contacts/export.csv">导出全部联系人 CSV</a> · <a href="/contacts?recycle=1">回收站</a></p><div class="table-wrap"><table><thead><tr><th>邮箱</th><th>姓名</th><th>分类</th><th>状态</th><th>操作</th></tr></thead><tbody>{items}</tbody></table></div></section>'''.format(
        count=count, alert=alert, csrf=csrf(user), search=h(search), options=options,
        items=''.join(items) or '<tr><td colspan="5">暂无联系人</td></tr>',
        **{key: 'selected' if status == key else '' for key in ('subscribed', 'unsubscribed', 'complained', 'invalid')})
    return page("联系人", content, user)


def templates_page(user, error="", selected_id=0, selected_version=None, preview_contact_id=0):
    with connect(DB_PATH) as db:
        templates = db.execute("SELECT * FROM templates WHERE owner_email=? ORDER BY updated_at DESC,id DESC", (user,)).fetchall()
        contacts = db.execute("SELECT id,email,name FROM contacts WHERE owner_email=? AND status='subscribed' AND deleted_at IS NULL ORDER BY id DESC LIMIT 200", (user,)).fetchall()
        categories = db.execute("SELECT name FROM contact_categories WHERE owner_email=? ORDER BY name", (user,)).fetchall()
        versions = db.execute("SELECT version,created_at FROM template_versions WHERE template_id=? ORDER BY version DESC", (selected_id,)).fetchall() if selected_id else []
        tests = db.execute("SELECT status,created_at FROM template_test_logs WHERE template_id=? AND owner_email=? ORDER BY id DESC LIMIT 10", (selected_id, user)).fetchall() if selected_id else []
        contact = db.execute("SELECT * FROM contacts WHERE id=? AND owner_email=?", (preview_contact_id, user)).fetchone() if preview_contact_id else None
    selected = get_template(DB_PATH, user, selected_id, selected_version) if selected_id else None
    list_html = ''.join('<li><a href="/templates?id={id}">{name} · v{version}</a></li>'.format(
        id=row["id"], name=h(row["name"]), version=h(get_template(DB_PATH, user, row["id"])["version"])) for row in templates)
    version_html = ''.join('<a href="/templates?id={}&version={}">v{} · {}</a> '.format(selected_id, row["version"], row["version"], local_time(row["created_at"])) for row in versions)
    contact_options = ''.join('<option value="{}" {}>{}</option>'.format(row["id"], 'selected' if preview_contact_id == row["id"] else '', h(row["email"])) for row in contacts)
    category_options = ''.join('<option value="{0}">{0}</option>'.format(h(row["name"])) for row in categories)
    test_logs = ''.join('<li>{} · {}</li>'.format(local_time(row["created_at"]), h(row["status"])) for row in tests)
    account = mailbox_settings(DB_PATH, user, SETTINGS)
    test_recipient = account.get("SMTP_FROM", "") if account else (user if "@" in user else "")
    preview = ''
    if selected and contact:
        try:
            subject, text, body_html = render_template(selected, contact)
            frame = ''
            if body_html:
                preview_doc = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
                               'img-src data:; style-src \'unsafe-inline\'; form-action \'none\'; '
                               'base-uri \'none\'">' + body_html)
                frame = ('<h3>HTML 效果预览</h3><p class="muted">外部图片在预览中不加载；实际邮件显示由收件人的邮箱客户端决定。</p>'
                         '<iframe class="template-preview" title="HTML 效果预览" sandbox="" '
                         'referrerpolicy="no-referrer" loading="lazy" srcdoc="{}"></iframe>').format(h(preview_doc))
            preview = '<section class="panel"><h2>变量预览 · {}</h2><strong>{}</strong><pre>{}</pre>{}<details><summary>HTML 源码预览</summary><pre>{}</pre></details></section>'.format(
                h(contact["email"]), h(subject), h(text), frame, h(body_html))
        except ValueError as exc:
            preview = '<div class="alert">{}</div>'.format(h(exc))
    copy_form = ''
    if selected:
        copy_form = '''<section class="panel"><h2>复制此版本</h2><p class="muted">以当前显示的 v{version} 创建独立模板；新模板从 v1 开始，不复制历史版本或测试记录。</p>
<form method="post" action="/templates/copy">{csrf}<input type="hidden" name="template_id" value="{id}"><input type="hidden" name="version" value="{version}"><label>新模板名称</label><input name="name" maxlength="120" value="{name}" required><button class="button secondary" type="submit">复制模板</button></form></section>'''.format(
            version=selected["version"], csrf=csrf(user), id=selected_id,
            name=h(selected["name"][:116] + "（副本）"))
    alert = '<div class="alert">{}</div>'.format(h(error)) if error else ''
    content = '''<nav class="back"><a href="/">← 返回批次</a></nav><section class="panel"><h1>邮件模板</h1>{alert}<p>模板按邮箱账号隔离；每次保存产生新版本，已创建的批次保留当时的邮件内容。</p><ul>{list_html}</ul><a href="/templates">新建模板</a> {campaign_link}</section>
<section class="panel"><h2>{heading}</h2><form method="post" action="/templates/save" id="template-form" data-editor-default="{editor_default}">{csrf}<input type="hidden" name="id" value="{id}"><label>模板名称</label><input name="name" maxlength="120" value="{name}" required><label>主题</label><input name="subject" maxlength="998" value="{subject}" required><label>纯文本正文</label><textarea name="body_text" rows="9" required>{text}</textarea><label for="body-html">HTML 正文（可选）</label>
<div class="editor-switch" role="group" aria-label="HTML 编辑方式"><button type="button" data-editor-mode="visual">可视化编辑</button><button type="button" data-editor-mode="source">HTML 源码</button></div>
<div id="visual-panel" hidden><div class="editor-toolbar" role="toolbar" aria-label="文字排版"><button type="button" data-editor-command="formatBlock" data-editor-value="p">正文</button><button type="button" data-editor-command="formatBlock" data-editor-value="h2">标题</button><button type="button" data-editor-command="bold"><strong>加粗</strong></button><button type="button" data-editor-command="italic"><em>斜体</em></button><button type="button" data-editor-command="insertUnorderedList">项目列表</button><button type="button" data-editor-command="insertOrderedList">编号列表</button><button type="button" data-editor-link>插入链接</button></div><div id="visual-editor" class="visual-editor" contenteditable="true" role="textbox" aria-label="邮件 HTML 正文" aria-multiline="true" data-placeholder="在这里编写邮件正文…"></div><p id="editor-status" class="muted" role="status" aria-live="polite"></p></div>
<textarea id="body-html" name="body_html" rows="9">{html}</textarea><p class="muted">可视化模式支持基础排版与链接。切换到可视化模式时，不支持的 HTML 标签或属性会被移除；复杂版式请保留源码模式。纯文本正文也需单独填写。</p><p>可用变量：{{{{邮箱}}}}、{{{{姓名}}}}、{{{{性别}}}}、{{{{生日}}}}、{{{{手机}}}}、{{{{地区}}}}、{{{{变量1}}}} 至 {{{{变量10}}}}。缺值时停止创建批次。</p><button class="button" type="submit">保存为新版本</button></form><p>历史版本：{versions}</p></section>
{copy_form}{preview}<section class="panel"><h2>预览与创建批次</h2><p>先保存模板，再选一个联系人预览变量；创建批次后仍需在批次页面明确启动发送。</p><form method="get" action="/templates"><input type="hidden" name="id" value="{id}"><select name="contact_id"><option value="">选择预览联系人</option>{contacts}</select><button type="submit">预览</button></form><form method="post" action="/campaigns/from-contacts">{csrf}<input type="hidden" name="template_id" value="{id}"><label>批次名称</label><input name="name" maxlength="120" required><label>联系人分类</label><select name="category"><option value="">全部可发送联系人</option>{categories}</select><button class="button" type="submit" {disabled}>创建待确认批次</button></form></section>
<section class="panel"><h2>发送测试</h2><p>这是一次真实 SMTP 提交，只允许发到你自己的发件邮箱。请先核对模板内容和收件邮箱。</p><form method="post" action="/templates/test">{csrf}<input type="hidden" name="template_id" value="{id}"><label>测试收件邮箱</label><input type="email" name="recipient" value="{test_recipient}" required><label>用于变量展开的联系人</label><select name="contact_id" required><option value="">请选择</option>{contacts}</select><label><input type="checkbox" name="confirm_test" value="1" required> 我确认发送这一封测试邮件</label><button type="submit" {disabled}>发送测试</button></form><p>最近测试记录：</p><ul>{test_logs}</ul></section><script src="editor.js" defer></script>'''.format(
        alert=alert, list_html=list_html, heading='编辑模板' if selected else '新建模板', csrf=csrf(user),
        campaign_link=('· <a href="/campaigns?template_id={}">按条件创建任务</a>'.format(selected_id) if selected else ''),
        id=selected_id or '', editor_default='source' if selected else 'visual',
        name=h(selected["name"] if selected else ''), subject=h(selected["subject"] if selected else ''),
        text=h(selected["body_text"] if selected else ''), html=h(selected["body_html"] if selected else ''),
        versions=version_html, preview=preview, copy_form=copy_form,
        contacts=contact_options, categories=category_options,
        disabled='' if selected else 'disabled', test_recipient=h(test_recipient), test_logs=test_logs)
    return page("邮件模板", content, user)


def campaigns_page(user, values=None, preview=None, error=""):
    values = values if values is not None else {}
    selected = set(str(value) for value in values.get("template_ids", []))
    with connect(DB_PATH) as db:
        templates = db.execute("""SELECT t.id,t.name,MAX(v.version) AS version FROM templates t
                                  JOIN template_versions v ON v.template_id=t.id
                                  WHERE t.owner_email=? GROUP BY t.id ORDER BY t.id""", (user,)).fetchall()
        categories = db.execute("SELECT name FROM contact_categories WHERE owner_email=? ORDER BY name", (user,)).fetchall()
    template_options = ''.join('<label class="campaign-option"><input type="checkbox" name="template_id" value="{}" {}> {} · v{}</label>'.format(
        row["id"], 'checked' if str(row["id"]) in selected else '', h(row["name"]), row["version"]) for row in templates)
    def category_options(key):
        return ''.join('<option value="{0}" {1}>{0}</option>'.format(
            h(row["name"]), 'selected' if row["name"] == values.get(key, "") else '') for row in categories)
    preview_html = ''
    if preview is not None:
        rows = ''.join('<tr><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            h(item[1]), (h(item[8]) + ' · ' if item[8] else '') + h(item[6]) + ' · v' + h(item[7]),
            h(item[2])) for item in preview[:10])
        preview_html = '''<section class="panel"><h2>筛选预览 · {} 位联系人</h2><p>这里只检查并展示前 10 封，不会排队或发送。创建草稿后请在批次页再核对全部邮件。</p>
<div class="table-wrap"><table><thead><tr><th>收件邮箱</th><th>模板/组</th><th>主题</th></tr></thead><tbody>{}</tbody></table></div></section>'''.format(len(preview), rows)
    alert = '<div class="alert">{}</div>'.format(h(error)) if error else ''
    content = '''<nav class="back"><a href="/">← 返回批次</a></nav><section class="panel"><h1>创建联系人任务</h1>{alert}
<p>仅选取当前账号下可发送、未删除且未退订的联系人；同一个邮箱在一个批次中只出现一次。普通多模板任务按下方显示顺序逐封轮流使用，创建时固定模板版本和正文。</p>
<form method="post" action="/campaigns">{csrf}<label>批次名称</label><input name="name" maxlength="120" value="{name}" required>
<h2>邮件模板</h2><p class="muted">选择 1 至 5 个；请先在“邮件模板”中保存模板。</p><div class="campaign-options">{templates}</div>
<label class="campaign-option"><input type="checkbox" name="ab_mode" value="1" {ab_checked}> A/B 对比：恰好选择 2 个模板，将联系人稳定、均衡分配给 A 和 B</label><p class="muted">预览和创建使用同一分组规则。结果仅提供对比建议，不会自动更改发送计划；点击建议需要在启动时启用点击记录。</p>
<h2>联系人条件</h2><div class="campaign-grid"><label>包含分类<select name="include_category"><option value="">不限分类</option>{include_categories}</select></label>
<label>排除分类<select name="exclude_category"><option value="">不排除分类</option>{exclude_categories}</select></label>
<label>邮箱包含字符<input name="email_contains" maxlength="100" value="{email_contains}" placeholder="例如：@example.com"></label>
<label>邮箱不包含字符<input name="email_excludes" maxlength="100" value="{email_excludes}" placeholder="例如：test"></label>
<label>排除最近已提交的联系人（天）<input type="number" name="exclude_sent_days" min="0" max="365" value="{exclude_sent_days}"></label>
<label>最近观测到打开（天，0 为不限）<input type="number" name="opened_days" min="0" max="365" value="{opened_days}"></label>
<label>最近观测到点击（天，0 为不限）<input type="number" name="clicked_days" min="0" max="365" value="{clicked_days}"></label></div>
<p class="muted">系统始终排除退订、投诉、无效、回收站和不再联系名单。这里的“已提交”指 SMTP 接受过该邮箱的邮件，不代表最终投递。</p>
<div class="actions"><button class="button secondary" type="submit" name="mode" value="preview">预览筛选</button><button class="button" type="submit" name="mode" value="create">创建待确认批次</button></div></form></section>{preview}'''.format(
        alert=alert, csrf=csrf(user), name=h(values.get("name", "")), templates=template_options or '<p>还没有模板。请先创建邮件模板。</p>',
        include_categories=category_options("include_category"), exclude_categories=category_options("exclude_category"),
        email_contains=h(values.get("email_contains", "")), email_excludes=h(values.get("email_excludes", "")),
        exclude_sent_days=h(values.get("exclude_sent_days", "0")),
        opened_days=h(values.get("opened_days", "0")), clicked_days=h(values.get("clicked_days", "0")),
        ab_checked='checked' if values.get("ab_mode") == "1" else '', preview=preview_html)
    return page("创建联系人任务", content, user)


def batch_page(batch_id, error="", user="__legacy_admin__"):
    with connect(DB_PATH) as db:
        batch = db.execute("SELECT * FROM batches WHERE id=? AND owner_email=?", (batch_id, user)).fetchone()
        if not batch:
            return None
        rows = db.execute("SELECT * FROM messages WHERE batch_id=? ORDER BY row_number", (batch_id,)).fetchall()
        stats = summary_for(db, batch_id)
        template_stats = db.execute("""SELECT variant,template_name,template_version,COUNT(*) AS total,
               SUM(CASE WHEN submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted,
               SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened,
               SUM(CASE WHEN submitted_at IS NOT NULL AND clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked,
               SUM(CASE WHEN bounced_at IS NOT NULL THEN 1 ELSE 0 END) AS bounced,
               SUM(CASE WHEN replied_at IS NOT NULL THEN 1 ELSE 0 END) AS replied,
               MAX(submitted_at) AS latest_submitted_at
               FROM messages WHERE batch_id=? AND template_id IS NOT NULL
               GROUP BY template_id,template_name,template_version,variant ORDER BY variant,template_id""", (batch_id,)).fetchall()
        events = db.execute("""SELECT e.kind,e.smtp_code,e.observed_at,e.detail,m.row_number
                               FROM delivery_events e JOIN messages m ON m.id=e.message_id
                               WHERE m.batch_id=? ORDER BY e.id DESC LIMIT 30""", (batch_id,)).fetchall()
    alert = '<div class="alert">{}</div>'.format(h(error).replace("\n", "<br>")) if error else ""
    offset = int(SETTINGS["UTC_OFFSET_HOURS"])
    default_time = (utcnow() + dt.timedelta(hours=offset, minutes=5)).strftime("%Y-%m-%dT%H:%M")
    controls = ""
    if batch["state"] == "draft":
        controls = '''<section class="panel launch"><div><div class="eyebrow">检查完成</div><h2>设置发送计划</h2><p class="muted">启动前请抽查正文、报价与收件人。开始后按顺序逐封发送。</p></div>
<form method="post" action="/batches/{id}/start">{csrf}<div class="form-grid"><div><label for="start">开始时间（UTC{offset:+d}）</label><input type="datetime-local" id="start" name="start" value="{default}"><label><input type="checkbox" name="immediate" value="1"> 立即开始</label></div>
<div><label for="interval">每封间隔（秒）</label><input type="number" id="interval" name="interval" min="{minimum}" max="86400" value="120" required></div></div>
<label for="reply-to">回复地址（可选）</label><input id="reply-to" type="email" name="reply_to" maxlength="254" placeholder="留空则回复到发件邮箱"><p class="muted">仅可填写与发件邮箱同域的地址。若回复送到其他邮箱，本账号的 IMAP 回复统计可能无法识别。</p>
<p class="muted">仅 HTML 邮件可启用。图片代理与安全扫描也可能触发记录。</p><label><input type="checkbox" name="track_open" value="1"> 记录首次打开请求</label><label><input type="checkbox" name="track_click" value="1"> 记录首次链接点击</label>
<button class="button" type="submit">确认并开始排队</button></form></section>'''.format(
            id=batch_id, csrf=csrf(user), offset=offset, default=default_time, minimum=h(SETTINGS["MIN_INTERVAL_SECONDS"]))
    elif batch["state"] in ("active", "paused"):
        action = "pause" if batch["state"] == "active" else "resume"
        label = "暂停剩余邮件" if action == "pause" else "继续发送"
        controls = '''<div class="actions"><form method="post" action="/batches/{id}/{action}">{csrf}<button class="button secondary">{label}</button></form>
<form method="post" action="/batches/{id}/cancel">{csrf}<button class="button subtle" onclick="return confirm('取消后，待发送邮件不会再发出。确定取消？')">取消剩余邮件</button></form></div>'''.format(
            id=batch_id, action=action, csrf=csrf(user), label=label)
    copy_controls = '''<section class="panel"><h2>复制为新任务</h2><p class="muted">复制收件人、主题和正文为待确认草稿；不会沿用发送时间、发送状态、回复地址、A/B 模式或跟踪设置。复制时重新检查退订、投诉、无效地址、回收站和不再联系名单；如有禁止发送的地址，复制将被拒绝，可重新筛选联系人创建任务。</p>
<form method="post" action="/batches/{id}/copy">{csrf}<label>新批次名称</label><input name="name" maxlength="120" value="{name}" required><button class="button secondary" type="submit">复制并检查</button></form></section>'''.format(
        id=batch_id, csrf=csrf(user), name=h(batch["name"] + "（副本）"))
    stat_html = "".join('<div class="stat"><strong>{}</strong><span>{}</span></div>'.format(stats[key], label) for key, label in
                        (("total", "总邮件"), ("queued", "待发送"), ("submitted", "已提交"),
                         ("opened", "观测打开"), ("clicked", "观测点击"),
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
        if row["opened_at"]:
            flags.append('<span class="pill replied">观测打开</span>')
        if row["clicked_at"]:
            flags.append('<span class="pill replied">观测点击</span>')
        row_html.append('''<tr><td class="row-no">{no}</td><td><strong>{email}</strong><div class="muted">{subject}</div>{template}<details><summary>查看正文</summary><pre>{body}</pre></details></td>
<td><span class="pill {raw_status}">{status}</span> {flags}</td><td>{scheduled}</td><td>{submitted}</td><td class="error-text">{error}</td></tr>'''.format(
            no=row["row_number"], email=h(row["recipient_email"]), subject=h(row["subject"]),
            template=('<div class="muted">模板：{} · v{}</div>'.format(h(row["template_name"]), h(row["template_version"]))
                      if row["template_name"] else ''),
            body=h(row["body"]), raw_status=h(row["status"]), status=h(status), flags=" ".join(flags),
            scheduled=local_time(row["scheduled_at"]), submitted=local_time(row["submitted_at"]),
            error=h(row["last_error"] or "—")))
    account = mailbox_settings(DB_PATH, user, SETTINGS)
    tracking_state = (" · 记录打开：{} · 记录点击：{}".format("开" if batch["track_open"] else "关",
                                                      "开" if batch["track_click"] else "关")
                      if batch["state"] != "draft" else "")
    reply_state = (" · 回复地址：{}".format(batch["reply_to"] or "发件邮箱")
                   if batch["state"] != "draft" else "")
    event_rows = ''.join('''<tr><td>{time}</td><td>{number}</td><td>{kind}</td><td>{code}</td><td>{detail}</td><td>{suggestion}</td></tr>'''.format(
        time=local_time(item["observed_at"]), number=item["row_number"],
        kind=h(EVENT_LABELS.get(item["kind"], item["kind"])), code=h(item["smtp_code"] or "—"),
        detail=h(item["detail"]), suggestion=h(event_suggestion(item["kind"], item["smtp_code"]))) for item in events)
    template_summary = ''
    if template_stats:
        template_rows = ''
        for item in template_stats:
            click_rate = ("{:.1f}%".format(100 * item["clicked"] / item["submitted"])
                          if batch["track_click"] and item["submitted"] else "—")
            template_rows += '<tr><td>{} · v{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
                (h(item["variant"]) + ' · ' if item["variant"] else '') + h(item["template_name"]),
                h(item["template_version"]), item["total"], item["submitted"],
                item["opened"], item["clicked"], click_rate, item["bounced"], item["replied"])
        guidance = ''
        if batch["ab_mode"]:
            latest = max((item["latest_submitted_at"] for item in template_stats if item["latest_submitted_at"]), default=None)
            _, message = ab_click_guidance(template_stats, batch["state"], batch["track_click"], latest)
            guidance = '<p class="muted">{}</p>'.format(h(message))
        intro = ('A/B 提示只比较已提交邮件中的首次点击，使用保守的 95% Wilson 区间，不会自动切换模板。'
                 if batch["ab_mode"] else '')
        template_summary = '''<section class="list-section"><h2>{title}</h2><p class="muted">打开/点击为首次请求观测值，可能包含代理或安全扫描；已提交只代表 SMTP 接受。{intro}</p>{guidance}
<div class="table-wrap"><table><thead><tr><th>模板</th><th>总邮件</th><th>已提交</th><th>观测打开</th><th>观测点击</th><th>观测点击率</th><th>退信</th><th>回复</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''.format(
            title='A/B 对比' if batch["ab_mode"] else '轮用模板结果', intro=intro,
            guidance=guidance, rows=template_rows)
    content = '''<nav class="back"><a href="/">← 返回批次列表</a></nav><section class="batch-head"><div><div class="eyebrow">批次 #{id} · {state}</div><h1>{name}</h1><p class="muted">创建于 {created} · 发件账号 {sender}{tracking_state}</p></div><a class="text-link" href="/batches/{id}/export.csv">导出明细 CSV ↗</a></section>
{alert}<section class="stats">{stats}</section>{controls}{copy_controls}{template_summary}<section class="list-section"><div class="section-heading"><h2>逐封结果</h2><span class="muted">已提交表示发信服务器已接受，不代表进入收件箱。</span></div>
<div class="table-wrap"><table><thead><tr><th>行</th><th>邮件</th><th>状态</th><th>计划时间</th><th>提交时间</th><th>原因</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section class="list-section"><div class="section-heading"><h2>发送日志</h2><a class="text-link" href="/batches/{id}/events.csv">导出全部日志 CSV ↗</a></div><p class="muted">显示最近 30 条；部署此功能后产生的新事件才会记录。SMTP 返回码及建议仅供排查，具体原因以服务器返回信息为准。</p>
<div class="table-wrap"><table><thead><tr><th>时间</th><th>行</th><th>事件</th><th>SMTP 码</th><th>详情</th><th>建议</th></tr></thead><tbody>{events}</tbody></table></div></section>'''.format(
        id=batch_id, state=STATE.get(batch["state"], batch["state"]), name=h(batch["name"]),
        created=local_time(batch["created_at"]), sender=h(account["SMTP_FROM"] if account else "未配置"),
        tracking_state=h(tracking_state + reply_state),
        alert=alert, stats=stat_html, controls=controls, copy_controls=copy_controls,
        template_summary=template_summary, rows="".join(row_html),
        events=event_rows or '<tr><td colspan="6">暂无新日志，既有邮件仍可查看逐封状态。</td></tr>')
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
        if not extra or "Referrer-Policy" not in extra:
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
        tracking = re.fullmatch(r"/track/(o|c)/([A-Za-z0-9_-]+\.[0-9a-f]{64})(\.gif)?", path)
        if tracking:
            kind, token, extension = tracking.groups()
            if (kind == "o") != bool(extension):
                self._send(404, "链接无效")
                return
            try:
                message_id, destination = verify_tracking_token(token, kind, SETTINGS["SESSION_SECRET"])
                if not record_tracking_event(DB_PATH, message_id, kind):
                    raise ValueError("链接无效")
            except ValueError:
                self._send(404, "链接无效")
                return
            if kind == "o":
                self._send(200, TRACKING_PIXEL, "image/gif", {"Referrer-Policy": "no-referrer"})
            else:
                self._send(303, b"", extra={"Location": destination, "Referrer-Policy": "no-referrer"})
            return
        if path == "/unsubscribe":
            token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            try:
                verify_unsubscribe_token(token, SETTINGS["SESSION_SECRET"])
            except ValueError:
                self._send(400, "退订链接无效")
                return
            content = '<section class="panel"><h1>停止接收邮件</h1><p>确认后将不再向此邮箱发送 Wismail 批量邮件。</p><form method="post" action="/unsubscribe"><input type="hidden" name="token" value="{}"><button class="button" type="submit">确认退订</button></form></section>'.format(h(token))
            self._send(200, page("确认退订", content))
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
        elif path == "/campaigns":
            query = parse_qs(urlparse(self.path).query)
            self._send(200, campaigns_page(self.current_user,
                                           {"template_ids": query.get("template_id", [])}))
        elif path in ("/reports", "/reports/export.csv"):
            query = parse_qs(urlparse(self.path).query)
            try:
                first, last = report_period(query.get("start", [""])[0], query.get("end", [""])[0])
                if path == "/reports":
                    self._send(200, reports_page(self.current_user, first.isoformat(), last.isoformat()))
                else:
                    data = report_data(DB_PATH, self.current_user, first, last, int(SETTINGS["UTC_OFFSET_HOURS"]))
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(["维度", "日期/批次", "SMTP 已接受", "观测到打开", "观测到点击", "发送失败/待核对", "检测到退信", "检测到回复"])
                    for kind, rows in (("日期", data["days"]), ("月份", data["months"])):
                        for label, values in rows:
                            writer.writerow([kind, label] + [values[key] for key in ("submitted", "opened", "clicked", "failed", "bounced", "replied")])
                    for values in data["batches"]:
                        writer.writerow(["批次", csv_safe(values["name"])] +
                                        [values[key] for key in ("submitted", "opened", "clicked", "failed", "bounced", "replied")])
                    self._send(200, b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="wismail-reports.csv"'})
            except ValueError as exc:
                self._send(400, page("统计报表", '<section class="panel"><h1>日期无效</h1><p>{}</p><a href="/reports">返回统计报表</a></section>'.format(h(exc)), self.current_user))
        elif path == "/templates":
            query = parse_qs(urlparse(self.path).query)
            try:
                body = templates_page(self.current_user,
                                      selected_id=int(query.get("id", ["0"])[0] or "0"),
                                      selected_version=int(query["version"][0]) if query.get("version") else None,
                                      preview_contact_id=int(query.get("contact_id", ["0"])[0] or "0"))
            except ValueError:
                self._send(404, "模板不存在")
                return
            self._send(200, body)
        elif path == "/contacts":
            query = parse_qs(urlparse(self.path).query)
            self._send(200, contacts_page(self.current_user, search=query.get("q", [""])[0],
                                          status=query.get("status", [""])[0],
                                          category=query.get("category", [""])[0],
                                          recycle=query.get("recycle", [""])[0] == "1"))
        elif path == "/contacts/export.csv":
            with connect(DB_PATH) as db:
                rows = db.execute("""SELECT c.*, (SELECT group_concat(g.name, ', ') FROM contact_category_links l
                                     JOIN contact_categories g ON g.id=l.category_id WHERE l.contact_id=c.id) AS categories
                                     FROM contacts c WHERE c.owner_email=? ORDER BY c.id""", (self.current_user,)).fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["邮箱", "姓名", "性别", "生日", "手机", "地区", "分类", "状态", "状态原因", "回收时间"] + ["变量{}".format(i) for i in range(1, 11)])
            for row in rows:
                custom = json.loads(row["custom_json"])
                values = [row["email"], row["name"], row["gender"], row["birthday"], row["phone"], row["region"],
                          row["categories"], row["status"], row["status_reason"], row["deleted_at"]]
                values += [custom.get("custom_" + str(i), "") for i in range(1, 11)]
                writer.writerow([csv_safe(value) for value in values])
            self._send(200, b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="wismail-contacts.csv"'})
        elif path == "/account" and oauth_login.configured(SETTINGS):
            self._send(200, account_page(self.current_user))
        elif path == "/static.css":
            with open(os.path.join(ROOT, "static.css"), "rb") as stream:
                self._send(200, stream.read(), "text/css; charset=utf-8")
        elif path == "/editor.js":
            with open(os.path.join(ROOT, "editor.js"), "rb") as stream:
                self._send(200, stream.read(), "text/javascript; charset=utf-8")
        elif path == "/template.xlsx":
            self._send(200, build_template(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       {"Content-Disposition": 'attachment; filename="wismail-upload-template.xlsx"'})
        else:
            match = re.match(r"^/batches/(\d+)(/export\.csv|/events\.csv)?$", path)
            if not match:
                self._send(404, page("未找到", '<div class="empty">页面不存在。<a href="/">返回首页</a></div>', self.current_user))
                return
            batch_id = int(match.group(1))
            if match.group(2) == "/events.csv":
                with connect(DB_PATH) as db:
                    owned = db.execute("SELECT 1 FROM batches WHERE id=? AND owner_email=?", (batch_id, self.current_user)).fetchone()
                    events = db.execute("""SELECT m.row_number,e.observed_at,e.kind,e.smtp_code,e.detail
                                           FROM delivery_events e JOIN messages m ON m.id=e.message_id
                                           WHERE m.batch_id=? ORDER BY e.id""", (batch_id,)).fetchall() if owned else None
                if events is None:
                    self._send(404, "批次不存在")
                    return
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["表格行号", "事件时间(UTC)", "事件", "SMTP 返回码", "详情", "建议"])
                for item in events:
                    writer.writerow([item["row_number"], item["observed_at"],
                                     csv_safe(EVENT_LABELS.get(item["kind"], item["kind"])), item["smtp_code"] or "",
                                     csv_safe(item["detail"]), csv_safe(event_suggestion(item["kind"], item["smtp_code"]))])
                self._send(200, b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="wismail-batch-{}-events.csv"'.format(batch_id)})
            elif match.group(2):
                with connect(DB_PATH) as db:
                    batch = db.execute("SELECT id FROM batches WHERE id=? AND owner_email=?", (batch_id, self.current_user)).fetchone()
                    rows = db.execute("""SELECT row_number,recipient_email,subject,template_name,template_version,variant,
                                               status,scheduled_at,submitted_at,opened_at,clicked_at,bounced_at,replied_at,last_error
                                        FROM messages WHERE batch_id=? ORDER BY row_number""", (batch_id,)).fetchall() if batch else None
                if rows is None:
                    self._send(404, "批次不存在")
                    return
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["表格行号", "收件邮箱", "邮件主题", "模板名称", "模板版本", "A/B组", "状态", "计划时间(UTC)", "提交时间(UTC)", "首次打开请求(UTC)", "首次点击(UTC)", "退信时间(UTC)", "回复时间(UTC)", "原因"])
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
        if self._route_path() == "/unsubscribe":
            try:
                owner, recipient = verify_unsubscribe_token(self._form().getfirst("token", ""), SETTINGS["SESSION_SECRET"])
                add_suppression(DB_PATH, recipient, "收件人退订", owner)
                with connect(DB_PATH) as db:
                    row = db.execute("SELECT id FROM contacts WHERE owner_email=? AND email=?", (owner, recipient)).fetchone()
                if row:
                    set_contact_status(DB_PATH, owner, row["id"], "unsubscribed", "收件人退订")
            except (ValueError, KeyError):
                self._send(400, "退订链接无效")
                return
            self._send(200, page("退订完成", '<section class="panel"><h1>已退订</h1><p>该邮箱不会再收到此账号的批量邮件。</p></section>'))
            return
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
            if path == "/templates/save":
                template_id, _ = save_template(DB_PATH, self.current_user, form.getfirst("name", ""),
                                               form.getfirst("subject", ""), form.getfirst("body_text", ""),
                                               form.getfirst("body_html", ""), int(form.getfirst("id", "0") or "0"))
                self._redirect("/templates?id={}".format(template_id))
                return
            if path == "/templates/copy":
                template_id, _ = copy_template(DB_PATH, self.current_user,
                                               int(form.getfirst("template_id", "0")),
                                               form.getfirst("name", ""),
                                               int(form.getfirst("version", "0")))
                self._redirect("/templates?id={}".format(template_id))
                return
            if path == "/campaigns/from-contacts":
                batch_id = create_contact_campaign(DB_PATH, self.current_user, form.getfirst("name", ""),
                                                   int(form.getfirst("template_id", "0")), form.getfirst("category", ""))
                self._redirect("/batches/{}".format(batch_id))
                return
            if path == "/campaigns":
                values = {key: form.getfirst(key, "") for key in
                          ("name", "include_category", "exclude_category", "email_contains",
                           "email_excludes", "exclude_sent_days", "opened_days", "clicked_days")}
                values["template_ids"] = form.getlist("template_id")
                values["ab_mode"] = form.getfirst("ab_mode", "")
                try:
                    if form.getfirst("mode", "") == "preview":
                        messages = prepare_contact_campaign(DB_PATH, self.current_user,
                                                            values["template_ids"], values)
                        self._send(200, campaigns_page(self.current_user, values, messages))
                    elif form.getfirst("mode", "") == "create":
                        batch_id = create_contact_campaign(DB_PATH, self.current_user,
                                                           values["name"], values["template_ids"], filters=values)
                        self._redirect("/batches/{}".format(batch_id))
                    else:
                        raise ValueError("请选择预览或创建批次")
                except ValueError as exc:
                    self._send(400, campaigns_page(self.current_user, values, error=str(exc)))
                return
            if path == "/templates/test":
                if form.getfirst("confirm_test", "") != "1":
                    raise ValueError("请明确确认发送测试邮件")
                template_id = int(form.getfirst("template_id", "0"))
                send_template_test(DB_PATH, self.current_user, template_id,
                                   int(form.getfirst("contact_id", "0")), form.getfirst("recipient", ""), SETTINGS)
                self._redirect("/templates?id={}".format(template_id))
                return
            if path == "/contacts/add":
                save_contact(DB_PATH, self.current_user, {key: form.getfirst(key, "") for key in ("email", "name", "categories")})
                self._redirect("/contacts")
                return
            if path == "/contacts/import":
                item = form["file"] if "file" in form else None
                if item is None or not getattr(item, "filename", None):
                    raise ValueError("请选择联系人文件")
                import_contacts(DB_PATH, self.current_user, parse_contacts_upload(item.filename, item.file.read(MAX_UPLOAD_BYTES + 1)))
                self._redirect("/contacts")
                return
            if path == "/contacts/status":
                set_contact_status(DB_PATH, self.current_user, int(form.getfirst("id", "0")), form.getfirst("status", ""))
                self._redirect("/contacts")
                return
            if path in ("/contacts/delete", "/contacts/restore"):
                set_contact_deleted(DB_PATH, self.current_user, int(form.getfirst("id", "0")), path.endswith("delete"))
                self._redirect("/contacts")
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
            match = re.match(r"^/batches/(\d+)/(start|pause|resume|cancel|copy)$", path)
            if not match:
                self._send(404, "操作不存在")
                return
            batch_id, action = int(match.group(1)), match.group(2)
            with connect(DB_PATH) as db:
                owned = db.execute("SELECT 1 FROM batches WHERE id=? AND owner_email=?", (batch_id, self.current_user)).fetchone()
            if not owned:
                self._send(404, "批次不存在")
                return
            if action == "copy":
                new_id = copy_batch(DB_PATH, self.current_user, batch_id, form.getfirst("name", ""))
                self._redirect("/batches/{}".format(new_id))
                return
            if action == "start":
                account = mailbox_settings(DB_PATH, self.current_user, SETTINGS)
                if not account or not all(account.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")):
                    raise ValueError("请先配置此邮箱的 SMTP 发件授权")
                interval = int(form.getfirst("interval", "0"))
                if interval < int(SETTINGS["MIN_INTERVAL_SECONDS"]):
                    raise ValueError("发送间隔小于服务器允许的最短间隔")
                track_open = form.getfirst("track_open", "") == "1"
                track_click = form.getfirst("track_click", "") == "1"
                if (track_open or track_click) and not tracking_ready(SETTINGS):
                    raise ValueError("跟踪需要 HTTPS 网站地址和至少 32 位的签名密钥")
                start = "now" if form.getfirst("immediate", "") == "1" else form.getfirst("start", "")
                schedule_batch(DB_PATH, batch_id, start, interval, int(SETTINGS["UTC_OFFSET_HOURS"]),
                               track_open, track_click, form.getfirst("reply_to", ""), account["SMTP_FROM"])
            elif action == "pause":
                set_batch_state(DB_PATH, batch_id, "paused")
            elif action == "resume":
                set_batch_state(DB_PATH, batch_id, "active")
            else:
                cancel_batch(DB_PATH, batch_id)
            self._redirect("/batches/{}".format(batch_id))
        except (ValueError, KeyError) as exc:
            if path in ("/templates/save", "/templates/copy", "/templates/test", "/campaigns/from-contacts"):
                self._send(400, templates_page(self.current_user, str(exc)))
                return
            if path.startswith("/contacts"):
                self._send(400, contacts_page(self.current_user, str(exc)))
                return
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
    if int(SETTINGS["DAILY_LIMIT"]) < 1 or int(SETTINGS["HOURLY_LIMIT"]) < 1 or int(SETTINGS["MIN_INTERVAL_SECONDS"]) < 1:
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
