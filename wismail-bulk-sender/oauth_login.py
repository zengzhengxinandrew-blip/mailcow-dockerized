"""Mailcow OAuth2 authorization-code login for the Wismail web module."""

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request


def configured(settings):
    return all(settings.get(key) for key in ("OAUTH_BASE_URL", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_REDIRECT_URI", "SESSION_SECRET"))


def _sign(value, secret):
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def new_state(settings):
    value = secrets.token_urlsafe(32)
    return value, value + "." + _sign(value, settings["SESSION_SECRET"])


def valid_state(query_state, cookie_state, settings):
    try:
        value, signature = cookie_state.rsplit(".", 1)
    except ValueError:
        return False
    return (hmac.compare_digest(value, query_state)
            and hmac.compare_digest(signature, _sign(value, settings["SESSION_SECRET"])))


def authorize_url(settings, state):
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": settings["OAUTH_CLIENT_ID"],
        "redirect_uri": settings["OAUTH_REDIRECT_URI"], "scope": "profile", "state": state,
    })
    return settings["OAUTH_BASE_URL"].rstrip("/") + "/oauth/authorize?" + query


def exchange_code(settings, code):
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": settings["OAUTH_REDIRECT_URI"],
        "client_id": settings["OAUTH_CLIENT_ID"],
        "client_secret": settings["OAUTH_CLIENT_SECRET"],
    }).encode("utf-8")
    request = urllib.request.Request(settings["OAUTH_BASE_URL"].rstrip("/") + "/oauth/token",
                                     data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=15) as response:
        token = json.load(response)
    access_token = token.get("access_token")
    if not access_token:
        raise ValueError("邮箱登录未返回访问令牌")
    profile_request = urllib.request.Request(settings["OAUTH_BASE_URL"].rstrip("/") + "/oauth/profile",
                                             headers={"Authorization": "Bearer " + access_token})
    with urllib.request.urlopen(profile_request, timeout=15) as response:
        profile = json.load(response)
    mailbox = str(profile.get("email") or "").strip().lower()
    if not profile.get("success") or str(profile.get("active")) != "1" or "@" not in mailbox:
        raise ValueError("邮箱账号未通过验证或已停用")
    return mailbox


def new_session(email_address, settings):
    payload = json.dumps({"email": email_address, "exp": int(time.time()) + 8 * 3600}, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return encoded + "." + _sign(encoded, settings["SESSION_SECRET"])


def read_session(cookie_value, settings):
    try:
        encoded, signature = cookie_value.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(encoded, settings["SESSION_SECRET"])):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload["exp"] <= time.time():
            return None
        return payload["email"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def csrf_token(email_address, settings):
    return _sign("csrf:" + email_address, settings["SESSION_SECRET"])
