"""Offline tests for the browser tool's URL/IP guard, gating and inbound auth."""

import asyncio
import ipaddress
import unittest
from unittest import mock

from browser_mcp.netguard import BlockedURLError, is_public_ip, validate_input_url


class ValidateInputUrlTest(unittest.TestCase):
    def test_accepts_public_https(self):
        for url in ("https://example.com", "https://example.com/path?q=1", "https://sub.example.be:443/"):
            self.assertEqual(validate_input_url(url), url)

    def test_rejects(self):
        bad = [
            "", "not a url", "http://example.com", "ftp://example.com", "file:///etc/passwd",
            "javascript:alert(1)", "data:text/html,hi", "chrome://settings",
            "https://localhost/", "https://foo.localhost/", "https://printer.local/",
            "https://metadata.google.internal/", "https://intranet/",
            "https://127.0.0.1/", "https://10.0.0.1/", "https://192.168.1.1/", "https://172.16.0.1/",
            "https://169.254.169.254/", "https://100.64.0.1/", "https://0.0.0.0/",
            "https://[::1]/", "https://[::ffff:127.0.0.1]/", "https://[fd00::1]/", "https://[fe80::1]/",
            "https://example.com:8080/", "https://user:pw@example.com/",
            "https://example.com/" + "a" * 3000,
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(BlockedURLError):
                validate_input_url(url)


class IsPublicIpTest(unittest.TestCase):
    def test_public(self):
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertTrue(is_public_ip(ipaddress.ip_address(ip)), ip)

    def test_non_public(self):
        for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "224.0.0.1", "::1",
                   "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:0a00:0001::1", "fc00::1"):
            self.assertFalse(is_public_ip(ipaddress.ip_address(ip)), ip)


class GateTest(unittest.TestCase):
    def _gate(self, env):
        from browser_mcp.tools import browser_tool_gate

        with mock.patch.dict("os.environ", env, clear=True):
            return browser_tool_gate()[0]

    def test_disabled_by_default(self):
        self.assertFalse(self._gate({}))

    def test_requires_auth_or_explicit_override(self):
        self.assertFalse(self._gate({"BROWSER_TOOL_ENABLED": "true"}))
        self.assertTrue(self._gate({"BROWSER_TOOL_ENABLED": "true", "MCP_INBOUND_AUTH": "api_key"}))
        self.assertTrue(self._gate({"BROWSER_TOOL_ENABLED": "true", "BROWSER_TOOL_ALLOW_UNAUTHENTICATED": "true"}))


class InboundAuthTest(unittest.TestCase):
    def _call(self, headers):
        from ads_mcp.inbound_auth import APIKeyASGIMiddleware

        sent = []

        async def app(scope, receive, send):
            sent.append("app")

        async def send(msg):
            sent.append(msg)

        mw = APIKeyASGIMiddleware(app, api_key="k" * 32)
        scope = {"type": "http", "method": "POST", "headers": headers}
        asyncio.run(mw(scope, None, send))
        return sent

    def test_rejects_missing_or_wrong_key(self):
        self.assertEqual(self._call([])[0]["status"], 401)
        self.assertEqual(self._call([(b"x-api-key", b"wrong")])[0]["status"], 401)

    def test_accepts_header_or_bearer(self):
        self.assertEqual(self._call([(b"x-api-key", b"k" * 32)]), ["app"])
        self.assertEqual(self._call([(b"authorization", b"Bearer " + b"k" * 32)]), ["app"])

    def test_off_by_default(self):
        from ads_mcp.inbound_auth import build_middleware

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(build_middleware(), [])


if __name__ == "__main__":
    unittest.main()
