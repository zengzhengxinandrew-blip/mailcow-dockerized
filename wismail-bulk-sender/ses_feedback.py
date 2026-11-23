"""Verify SNS envelopes and extract SES delivery feedback."""

import base64
import json
import re
import urllib.request
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding


SNS_HOST = "sns.ap-southeast-2.amazonaws.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _fetch(url):
    with urllib.request.build_opener(_NoRedirect).open(url, timeout=5) as response:
        return response.read(65537)


def _sns_url(url, certificate=False):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != SNS_HOST or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("SNS URL 不可信")
    if certificate and (not re.fullmatch(r"/SimpleNotificationService-[A-Za-z0-9]+\.pem", parsed.path)
                        or parsed.query):
        raise ValueError("SNS 证书 URL 无效")
    return url


def verify_sns(body, topic_arn, header_type="", fetch=_fetch):
    if not topic_arn or not re.fullmatch(r"arn:aws:sns:ap-southeast-2:\d{12}:[A-Za-z0-9_-]+", topic_arn):
        raise ValueError("未配置 Sydney 区域 SNS Topic ARN")
    if len(body) > 262144:
        raise ValueError("SNS 消息过大")
    try:
        envelope = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SNS 消息不是有效 JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("SNS 消息结构无效")
    kind = envelope.get("Type")
    if kind not in ("Notification", "SubscriptionConfirmation") or (header_type and header_type != kind):
        raise ValueError("SNS 消息类型无效")
    if envelope.get("TopicArn") != topic_arn:
        raise ValueError("SNS Topic ARN 不匹配")
    version = envelope.get("SignatureVersion")
    if version not in ("1", "2"):
        raise ValueError("SNS 签名版本无效")
    fields = (["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"] if kind == "Notification"
              else ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"])
    mandatory = set(fields) - {"Subject"}
    if any(not isinstance(envelope.get(name), str) or not envelope[name] for name in mandatory):
        raise ValueError("SNS 必填字段缺失")
    signed = "".join(name + "\n" + envelope[name] + "\n" for name in fields if name in envelope).encode("utf-8")
    cert_url = _sns_url(envelope.get("SigningCertURL", ""), certificate=True)
    certificate_bytes = fetch(cert_url)
    if len(certificate_bytes) > 65536:
        raise ValueError("SNS 证书过大")
    try:
        certificate = x509.load_pem_x509_certificate(certificate_bytes, default_backend())
        signature = base64.b64decode(envelope["Signature"], validate=True)
        certificate.public_key().verify(signature, signed, padding.PKCS1v15(),
                                        hashes.SHA256() if version == "2" else hashes.SHA1())
    except (ValueError, KeyError, InvalidSignature) as exc:
        raise ValueError("SNS 签名无效") from exc
    if kind == "SubscriptionConfirmation":
        _sns_url(envelope["SubscribeURL"])
    return envelope


def confirm_sns_subscription(envelope, fetch=_fetch):
    if envelope["Type"] != "SubscriptionConfirmation":
        raise ValueError("不是订阅确认消息")
    return fetch(_sns_url(envelope["SubscribeURL"]))


def ses_feedback(envelope):
    if envelope["Type"] != "Notification":
        return None
    try:
        event = json.loads(envelope["Message"])
    except json.JSONDecodeError as exc:
        raise ValueError("SES 事件 JSON 无效") from exc
    if not isinstance(event, dict):
        raise ValueError("SES 事件结构无效")
    kind = event.get("eventType") or event.get("notificationType")
    if kind not in ("Delivery", "Bounce", "Complaint"):
        return None
    mail = event.get("mail") or {}
    if not isinstance(mail, dict) or not isinstance(mail.get("headers", []), list):
        raise ValueError("SES 邮件头结构无效")
    original_ids = [str(header.get("value", "")).strip() for header in mail.get("headers", [])
                    if isinstance(header, dict) and str(header.get("name", "")).lower() == "message-id"]
    # SES also publishes feedback for mail sent outside Wismail. Such mail can
    # have no original Message-ID (or a different format), so acknowledge it
    # without changing a Wismail message or making SNS retry it.
    if len(original_ids) != 1 or not re.fullmatch(r"<[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+>", original_ids[0]):
        return None
    key = {"Delivery": "delivery", "Bounce": "bounce", "Complaint": "complaint"}[kind]
    values = event.get(key) or {}
    if not isinstance(values, dict):
        raise ValueError("SES 反馈结构无效")
    if kind == "Delivery":
        recipients = values.get("recipients", [])
    else:
        recipients = [row.get("emailAddress", "") for row in values.get(
            "bouncedRecipients" if kind == "Bounce" else "complainedRecipients", []) if isinstance(row, dict)]
    if not isinstance(recipients, list):
        raise ValueError("SES 收件地址结构无效")
    recipients = sorted({str(item).strip().lower() for item in recipients if isinstance(item, str) and "@" in item})
    return {"sns_message_id": envelope["MessageId"], "kind": kind.lower(),
            "message_id": original_ids[0], "recipients": recipients,
            "permanent": kind == "Bounce" and values.get("bounceType") == "Permanent"}
