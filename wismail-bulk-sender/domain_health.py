"""Read-only public DNS and HTTPS checks for a mailbox domain."""

import socket
import ssl
from urllib.parse import urlparse

import dns.resolver


def _records(name, kind, resolver):
    try:
        return [str(item).strip('"') for item in resolver.resolve(name, kind, lifetime=3)]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError) as exc:
        return None


def _check(label, values, expected=None):
    if values is None:
        return (label, "无法查询", "DNS 查询失败或超时")
    matching = [value for value in values if expected is None or value.lower().startswith(expected.lower())]
    return (label, "检测到" if matching else "未检测到", "; ".join(matching[:3])[:400] or "—")


def domain_health(mailbox_email, settings, resolver=None, tls_probe=None):
    """Return observable records; never infer final deliverability from DNS alone."""
    domain = mailbox_email.rsplit('@', 1)[-1].strip().lower()
    if '@' not in mailbox_email or not domain or len(domain) > 253:
        raise ValueError("无效的邮箱域名")
    resolver = resolver or dns.resolver.Resolver()
    selector = settings.get("DKIM_SELECTOR", "dkim").strip() or "dkim"
    if not selector.replace('-', '').replace('_', '').isalnum():
        raise ValueError("DKIM selector 无效")
    rows = [
        _check("MX", _records(domain, "MX", resolver)),
        _check("SPF", _records(domain, "TXT", resolver), "v=spf1"),
        _check("DKIM", _records(f"{selector}._domainkey.{domain}", "TXT", resolver), "v=DKIM1"),
        _check("DMARC", _records(f"_dmarc.{domain}", "TXT", resolver), "v=DMARC1"),
    ]
    raw_tracking = settings.get("TRACKING_BASE_URL") or settings.get("OAUTH_BASE_URL", "")
    tracking_host = urlparse(raw_tracking).hostname if raw_tracking else None
    if tracking_host:
        rows.append(_check("跟踪域名 DNS", _records(tracking_host, "A", resolver)))
        if tls_probe is None:
            tls_probe = _tls_certificate
        try:
            tls_probe(tracking_host)
            rows.append(("跟踪域名 HTTPS", "证书有效", tracking_host))
        except (OSError, ssl.SSLError, ssl.CertificateError) as exc:
            rows.append(("跟踪域名 HTTPS", "无法验证", type(exc).__name__))
    else:
        rows.append(("跟踪域名 HTTPS", "未配置", "—"))
    return domain, rows


def _tls_certificate(host):
    context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=3) as connection:
        with context.wrap_socket(connection, server_hostname=host):
            pass
