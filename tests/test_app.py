import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class UaTest(unittest.TestCase):
    def test_clash_family(self):
        for ua in (
            "clash-verge/v2.4.9",
            "FlClash/v0.9.2 clash-verge Platform/windows",
            "clash.meta/1.19.0",
            "Mihomo/1.19.0",
        ):
            self.assertEqual(app.classify_ua(ua), "clash", ua)

    def test_other(self):
        for ua in ("Shadowrocket/2.2.0", "v2rayN/6.31", "SFA/1.11", ""):
            self.assertEqual(app.classify_ua(ua), "raw", ua)


class UserinfoTest(unittest.TestCase):
    def test_normal(self):
        usage = app.parse_userinfo("upload=100; download=200; total=1000; expire=1700000000")
        self.assertTrue(usage["present"])
        self.assertEqual(
            (usage["upload"], usage["download"], usage["total"], usage["used"]),
            (100, 200, 1000, 300),
        )
        self.assertEqual(usage["remaining"], 700)
        self.assertAlmostEqual(usage["percent"], 30.0)
        self.assertFalse(usage["unlimited"])

    def test_unlimited(self):
        usage = app.parse_userinfo("upload=1; download=2; total=0; expire=0")
        self.assertTrue(usage["unlimited"])
        self.assertIsNone(usage["remaining"])
        self.assertEqual(usage["total_h"], "不限")

    def test_empty_and_bad(self):
        self.assertFalse(app.parse_userinfo(None)["present"])
        with self.assertLogs("xui-sub", level="WARNING"):
            self.assertFalse(app.parse_userinfo("garbage")["present"])
        with self.assertLogs("xui-sub", level="WARNING"):
            self.assertFalse(app.parse_userinfo("upload=x")["present"])


class HeaderTest(unittest.TestCase):
    def test_crlf(self):
        self.assertEqual(app._header_safe("a\r\nb"), "a  b")

    def test_non_ascii(self):
        self.assertEqual(app._header_safe("节点"), "%E8%8A%82%E7%82%B9")


class RedactTest(unittest.TestCase):
    def test_token(self):
        self.assertEqual(app._redact("abcdefgh"), "abcd…")
        self.assertEqual(app._redact("abc"), "…")

    def test_path(self):
        self.assertEqual(app._redact_path("/sub/abcdefgh?x=1"), "/sub/abcd…")
        self.assertEqual(app._redact_path("/RDWeb"), "/RDWeb")


class RenderTest(unittest.TestCase):
    PROXY = {
        "name": "node-1",
        "type": "vless",
        "server": "203.0.113.10",
        "port": 443,
        "uuid": "00000000-0000-0000-0000-000000000000",
        "network": "tcp",
        "tls": True,
        "udp": True,
        "servername": "example.com",
        "client-fingerprint": "chrome",
        "reality-opts": {"public-key": "test", "short-id": "0123"},
    }

    def test_clash_template(self):
        env = app.build_env(str(ROOT / "templates"))
        usage = app.parse_userinfo("upload=0; download=0; total=0; expire=0")
        text = env.get_template("clash.yaml.j2").render(
            proxies=[self.PROXY],
            usage=usage,
            title="test",
            generated_at="2026-01-01 00:00:00 +0000",
            update_interval=12,
            sub_id="test",
        )
        document = yaml.safe_load(text)
        self.assertEqual(document["proxies"][0]["name"], "node-1")
        self.assertEqual(
            [group["name"] for group in document["proxy-groups"]],
            ["🚀 节点选择", "♻️ 自动选择"],
        )
        self.assertIn("MATCH,🚀 节点选择", document["rules"])
        self.assertEqual(document["dns"]["enhanced-mode"], "fake-ip")
        self.assertIn("localhost", document["dns"]["fake-ip-filter"])
        self.assertNotIn("localhost.", document["dns"]["fake-ip-filter"])


if __name__ == "__main__":
    unittest.main()
