import unittest

import dns.resolver

from domain_health import domain_health


class Resolver:
    def __init__(self, records):
        self.records = records

    def resolve(self, name, kind, lifetime):
        assert lifetime <= 3
        value = self.records.get((name, kind), [])
        if isinstance(value, Exception):
            raise value
        return value


class DomainHealthTests(unittest.TestCase):
    def test_reports_observed_records_without_claiming_delivery(self):
        records = {
            ("wiskind.top", "MX"): ["10 mail.wiskind.top."],
            ("wiskind.top", "TXT"): ['"v=spf1 mx ~all"', '"unrelated"'],
            ("dkim._domainkey.wiskind.top", "TXT"): ['"v=DKIM1; p=abc"'],
            ("_dmarc.wiskind.top", "TXT"): ['"v=DMARC1; p=none"'],
            ("track.wiskind.top", "A"): ["203.0.113.10"],
        }
        domain, rows = domain_health("user01@wiskind.top", {
            "OAUTH_BASE_URL": "https://mail.wiskind.top",
            "TRACKING_BASE_URL": "https://track.wiskind.top",
        }, Resolver(records), lambda host: self.assertEqual(host, "track.wiskind.top"))
        self.assertEqual(domain, "wiskind.top")
        self.assertEqual([row[1] for row in rows], ["检测到"] * 5 + ["证书有效"])

    def test_missing_or_unavailable_records_are_distinct(self):
        records = {("wiskind.top", "TXT"): dns.resolver.Timeout()}
        _, rows = domain_health("user01@wiskind.top", {"OAUTH_BASE_URL": ""}, Resolver(records))
        self.assertEqual(rows[0][1], "未检测到")
        self.assertEqual(rows[1][1], "无法查询")
        self.assertEqual(rows[-1][1], "未配置")


if __name__ == "__main__":
    unittest.main()
