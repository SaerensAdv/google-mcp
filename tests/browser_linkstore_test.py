"""Tests for the temporary screenshot download links (browser_mcp.linkstore)."""

import logging
import os
import unittest
from unittest import mock

from browser_mcp import linkstore
from browser_mcp.linkstore import LinkStore

JPEG = b"\xff\xd8\xff" + b"x" * 1000


class LinkStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = LinkStore()
        self._timer = mock.patch.object(LinkStore, "_schedule_purge", lambda self: None)
        self._timer.start()

    def tearDown(self):
        self._timer.stop()
        self.store.purge_expired(now=float("inf"))
        if self.store._dir:
            import shutil
            shutil.rmtree(self.store._dir, ignore_errors=True)

    def test_roundtrip_and_repeated_fetch(self):
        token, _ = self.store.put(JPEG)
        self.assertGreaterEqual(len(token), 32)  # 192-bit token_urlsafe
        self.assertEqual(self.store.get(token), JPEG)
        self.assertEqual(self.store.get(token), JPEG)  # not consumed by a first fetch

    def test_file_is_private_and_outside_workspace(self):
        self.store.put(JPEG)
        item = next(iter(self.store._items.values()))
        self.assertFalse(os.path.abspath(item.path).startswith(os.getcwd()))
        self.assertEqual(os.stat(item.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(item.path)).st_mode & 0o777, 0o700)

    def test_expiry_checked_on_request_even_before_cleanup(self):
        token, expires = self.store.put(JPEG)
        with mock.patch("browser_mcp.linkstore.time.time", return_value=expires + 0.01):
            self.assertIsNone(self.store.get(token))
        self.assertEqual(self.store.stats(), (0, 0))

    def test_ttl_capped_at_10_minutes(self):
        self.assertLessEqual(linkstore.TTL_S, 600)
        _, expires = self.store.put(JPEG)
        self.assertLessEqual(expires - linkstore.time.time(), 600.5)

    def test_unknown_and_malformed_tokens(self):
        self.store.put(JPEG)
        for bad in ["A" * 32, "", "../../etc/passwd", "..%2f..%2fx", "a/b", "A" * 33,
                    next(iter(self.store._items.values())).path]:
            self.assertIsNone(self.store.get(bad), bad)

    def test_purge_removes_files(self):
        token, expires = self.store.put(JPEG)
        path = self.store._items[token].path
        self.store.purge_expired(now=expires + 1)
        self.assertFalse(os.path.exists(path))

    def test_item_limit_evicts_oldest(self):
        with mock.patch.object(linkstore, "MAX_ITEMS", 3):
            tokens = [self.store.put(JPEG)[0] for _ in range(5)]
        self.assertEqual(self.store.stats()[0], 3)
        self.assertIsNone(self.store.get(tokens[0]))
        self.assertIsNone(self.store.get(tokens[1]))
        self.assertEqual(self.store.get(tokens[4]), JPEG)
        on_disk = os.listdir(self.store._dir)
        self.assertEqual(len(on_disk), 3)

    def test_byte_limit(self):
        with mock.patch.object(linkstore, "MAX_TOTAL_BYTES", 2500):
            self.store.put(JPEG)
            self.store.put(JPEG)
            self.store.put(JPEG)
            self.assertLessEqual(self.store.stats()[1], 2500)
            self.assertIsNone(self.store.put(b"x" * 3000))  # single image too large


class StaleDirTest(unittest.TestCase):
    def test_old_orphaned_dirs_removed_recent_kept(self):
        import tempfile, time
        old = tempfile.mkdtemp(prefix="shotlinks-")
        open(os.path.join(old, "a.jpg"), "wb").close()
        recent = tempfile.mkdtemp(prefix="shotlinks-")
        past = time.time() - 3600
        os.utime(old, (past, past))
        linkstore._remove_stale_dirs()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))
        os.rmdir(recent)


class ConfigTest(unittest.TestCase):
    def test_base_url_must_be_explicit_https_origin(self):
        cases = {
            "": None,
            "https://sa.example.app": "https://sa.example.app",
            "https://sa.example.app/": "https://sa.example.app",
            "http://sa.example.app": None,
            "https://sa.example.app/path": None,
            "https://user@sa.example.app": None,
        }
        for raw, want in cases.items():
            with mock.patch.dict(os.environ, {"BROWSER_SCREENSHOT_BASE_URL": raw}):
                self.assertEqual(linkstore.public_base_url(), want, raw)

    def test_host_allowlist_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BROWSER_SCREENSHOT_LINK_HOSTS", None)
            self.assertTrue(linkstore.link_allowed_for("https://abonnement.website/"))
            self.assertFalse(linkstore.link_allowed_for("https://example.com/"))
            self.assertFalse(linkstore.link_allowed_for("https://abonnement.website.evil.com/"))

    def test_access_log_redaction(self):
        f = linkstore._RedactLinkTokens()
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                '%s - "%s %s HTTP/%s" %d', ("1.2.3.4", "GET",
                                "/browser-screenshots/SECRETTOKEN123", "1.1", 200), None)
        f.filter(rec)
        self.assertNotIn("SECRETTOKEN123", rec.getMessage())


if __name__ == "__main__":
    unittest.main()
