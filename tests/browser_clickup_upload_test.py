"""Tests for browser_screenshot_to_clickup (browser_mcp.clickup_upload) against a fake ClickUp."""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import httpx

from browser_mcp import clickup_upload as cu
from browser_mcp.capture import CaptureError, CaptureResult

TOKEN = "pk_test_token_ABCDEFGHIJKLMNOP"
KEY = "upload-key-0123456789abcdefghij"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 5000 + b"\xff\xd9"
ENV = {
    "CLICKUP_UPLOAD_ENABLED": "true",
    "CLICKUP_API_TOKEN": TOKEN,
    "SCREENSHOT_UPLOAD_KEY": KEY,
    "CLICKUP_UPLOAD_ALLOWED_LIST_IDS": "900100",
}


class FakeRequest:
    def __init__(self, headers):
        self.headers = httpx.Headers(headers)


class FakeClickUp:
    def __init__(self):
        self.tasks = {
            "86ok": {"id": "86ok", "url": "https://app.clickup.com/t/86ok",
                     "list": {"id": "900100"}, "attachments": []},
            "86other": {"id": "86other", "url": "https://app.clickup.com/t/86other",
                        "list": {"id": "555"}, "attachments": []},
        }
        self.files = {}
        self.uploads = 0
        self.calls = []
        self.upload_status = 200
        self.store_on_error = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), request.headers.get("authorization")))
        m = re.match(r"^/api/v2/task/(\w+)(/attachment)?$", request.url.path)
        if request.url.host == "api.clickup.com" and m:
            task = self.tasks.get(m.group(1))
            if task is None:
                return httpx.Response(404, json={"err": "not found"})
            if request.method == "GET" and not m.group(2):
                return httpx.Response(200, json=task)
            if request.method == "POST" and m.group(2):
                body = request.read()
                fn = re.search(rb'filename="([^"]+)"', body).group(1).decode()
                start = body.index(b"\r\n\r\n") + 4
                data = body[start:body.rindex(b"\r\n--")]
                att_id = f"att{len(self.files) + 1}.jpg"
                url = f"https://t1.p.clickup-attachments.com/t1/{att_id}/{fn}"
                if self.upload_status == 200 or self.store_on_error:
                    self.uploads += 1
                    self.files[url] = data
                    task["attachments"].append({"id": att_id, "title": fn, "url": url,
                                                "mimetype": "image/jpeg", "size": len(data)})
                if self.upload_status != 200:
                    return httpx.Response(self.upload_status, json={"err": "x"})
                return httpx.Response(200, json={"id": att_id, "title": fn, "url": url,
                                                 "extension": "jpg", "version": "0", "date": 1})
        if request.url.host.endswith("clickup-attachments.com") and str(request.url) in self.files:
            return httpx.Response(200, content=self.files[str(request.url)])
        return httpx.Response(404)


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        self.fake = FakeClickUp()
        cu._transport = httpx.MockTransport(self.fake.handler)
        self.dir = tempfile.mkdtemp()
        self.registry = cu.Registry(os.path.join(self.dir, "reg.json"))
        self.captures = 0
        self.capture_fail = False

    def tearDown(self):
        cu._transport = None
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _capture(self, url, device, full_page):
        self.captures += 1
        if self.capture_fail:
            raise CaptureError("Navigatie mislukt")
        return CaptureResult(image=JPEG, metadata={
            "final_url": url, "captured_at": datetime(2026, 9, 27, 12, 0, 5, tzinfo=timezone.utc).isoformat(),
            "viewport": {"width": 1440, "height": 900}, "capture_mode": "viewport",
            "image_size_px": {"width": 1440, "height": 900}, "scale": 1.0,
            "browser_sandbox": True, "warnings": [],
        })

    def run_upload(self, headers=None, registry=None, **kw):
        args = dict(url="https://abonnement.website/?utm=x", task_id="86ok", device="desktop",
                    full_page=False, request_id="req-00000001")
        args.update(kw)
        req = FakeRequest({cu.HEADER_NAME: KEY} if headers is None else headers)
        return asyncio.run(cu.screenshot_to_clickup(
            request=req, capture_fn=self._capture, registry=registry or self.registry, **args))

    # --- refusals before any capture/upload
    def assert_refused_early(self, fn):
        with self.assertRaises(cu.UploadRefused):
            fn()
        self.assertEqual(self.captures, 0)
        self.assertEqual(self.fake.uploads, 0)

    def test_missing_header_refused(self):
        self.assert_refused_early(lambda: self.run_upload(headers={}))
        self.assertEqual(self.fake.calls, [])

    def test_wrong_header_refused(self):
        self.assert_refused_early(lambda: self.run_upload(headers={cu.HEADER_NAME: KEY + "x"}))
        self.assertEqual(self.fake.calls, [])

    def test_no_http_request_refused(self):
        with self.assertRaises(cu.UploadRefused):
            asyncio.run(cu.screenshot_to_clickup(
                request=None, url="https://abonnement.website/", task_id="86ok", device="desktop",
                full_page=False, request_id="req-00000001", capture_fn=self._capture,
                registry=self.registry))
        self.assertEqual(self.captures, 0)

    def test_task_outside_allowed_list_refused(self):
        self.assert_refused_early(lambda: self.run_upload(task_id="86other"))

    def test_unknown_task_refused(self):
        self.assert_refused_early(lambda: self.run_upload(task_id="86nope"))

    def test_invalid_inputs_refused(self):
        self.assert_refused_early(lambda: self.run_upload(task_id="../team/1"))
        self.assert_refused_early(lambda: self.run_upload(request_id="x"))
        self.assert_refused_early(lambda: self.run_upload(url="http://abonnement.website/"))
        self.assert_refused_early(lambda: self.run_upload(url="https://127.0.0.1/"))

    # --- happy path + duplicate protection
    def test_upload_verified_and_content_identical(self):
        r = self.run_upload()
        self.assertTrue(r["capture_ok"] and r["upload_ok"] and r["upload_verified"])
        self.assertEqual(r["attachment_id"], "att1.jpg")
        self.assertTrue(r["attachment_url"].startswith("https://t1.p.clickup-attachments.com/"))
        self.assertEqual(r["content_check"]["content"], "identiek aan geüploade JPEG")
        self.assertTrue(r["content_verified"])
        self.assertRegex(r["filename"], r"^abonnement\.website_desktop_20260927T120005Z_r[0-9a-f]{10}-[0-9a-f]{8}\.jpg$")
        self.assertEqual(r["task_url"], "https://app.clickup.com/t/86ok")
        self.assertEqual(self.fake.files[r["attachment_url"]], JPEG)

    def test_same_request_id_same_input_no_second_upload(self):
        first = self.run_upload()
        second = self.run_upload()
        self.assertEqual(self.fake.uploads, 1)
        self.assertEqual(self.captures, 1)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["attachment_id"], first["attachment_id"])

    def test_same_request_id_other_input_refused(self):
        self.run_upload()
        with self.assertRaises(cu.UploadRefused):
            self.run_upload(device="mobile")
        self.assertEqual(self.fake.uploads, 1)

    def test_lost_registry_still_finds_existing_attachment(self):
        self.run_upload()
        fresh = cu.Registry(os.path.join(self.dir, "other.json"))
        r = self.run_upload(registry=fresh)
        self.assertTrue(r["deduplicated"])
        self.assertEqual(self.fake.uploads, 1)
        with self.assertRaises(cu.UploadRefused):  # other input, same request_id
            self.run_upload(registry=cu.Registry(os.path.join(self.dir, "third.json")), full_page=True)

    def test_registered_upload_not_repeated_even_if_attachment_removed(self):
        self.run_upload()
        self.fake.tasks["86ok"]["attachments"].clear()
        r = self.run_upload()
        self.assertTrue(r["deduplicated"])
        self.assertFalse(r["upload_verified"])
        self.assertEqual(self.fake.uploads, 1)
        self.assertEqual(self.captures, 1)

    def test_uncertain_upload_not_visible_is_not_retried(self):
        self.fake.upload_status = 502  # ClickUp errors and nothing is visible (yet)
        r = self.run_upload()
        self.assertFalse(r["upload_ok"])
        self.fake.upload_status = 200
        with self.assertRaises(cu.UploadRefused):
            self.run_upload()  # same request_id: fail closed, no second POST
        self.assertEqual(self.fake.uploads, 0)
        self.assertEqual(self.captures, 1)

    # --- failures
    def test_capture_failure_reported_separately(self):
        self.capture_fail = True
        r = self.run_upload()
        self.assertFalse(r["capture_ok"])
        self.assertFalse(r["upload_ok"])
        self.assertEqual(self.fake.uploads, 0)

    def test_upload_failure_reported_separately(self):
        self.fake.upload_status = 400
        r = self.run_upload()
        self.assertTrue(r["capture_ok"])
        self.assertFalse(r["upload_ok"])
        self.assertIn("upload mislukt", r["error"])

    def test_uncertain_upload_checks_task_first(self):
        self.fake.upload_status = 502
        self.fake.store_on_error = True  # ClickUp did store it despite the error
        r = self.run_upload()
        self.assertTrue(r["upload_ok"])
        self.assertEqual(self.fake.uploads, 1)
        self.fake.upload_status = 200
        again = self.run_upload()  # retry must not upload again
        self.assertTrue(again["deduplicated"])
        self.assertEqual(self.fake.uploads, 1)

    # --- secrets
    def test_no_secrets_in_result_or_logs(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("t").debug("start")
            r = self.run_upload()
        blob = json.dumps(r) + "\n".join(logs.output)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(KEY, blob)

    def test_token_only_sent_to_clickup_api(self):
        self.run_upload()
        for method, url, auth in self.fake.calls:
            if httpx.URL(url).host == "api.clickup.com":
                self.assertEqual(auth, TOKEN)
            else:
                self.assertIsNone(auth, url)

    def test_secrets_not_in_browser_env(self):
        from browser_mcp.capture import _browser_env
        env = _browser_env()
        self.assertNotIn("CLICKUP_API_TOKEN", env)
        self.assertNotIn("SCREENSHOT_UPLOAD_KEY", env)
        self.assertNotIn(TOKEN, json.dumps(env))
        self.assertNotIn(KEY, json.dumps(env))

    def test_registry_file_private_and_bounded(self):
        with mock.patch.object(cu, "REGISTRY_MAX_ENTRIES", 3):
            for i in range(5):
                self.registry.put(f"req-{i:08d}", {"fingerprint": "x"})
        with open(self.registry.path) as f:
            self.assertEqual(len(json.load(f)), 3)
        self.assertEqual(os.stat(self.registry.path).st_mode & 0o777, 0o600)


class MultiListAllowlistTest(UploadTest):
    """Two allowed Home Lists (e.g. Opportunities + Lead Pipeline) via the same env var."""

    def setUp(self):
        super().setUp()
        self.env2 = mock.patch.dict(os.environ, {"CLICKUP_UPLOAD_ALLOWED_LIST_IDS": "900100, 900200"})
        self.env2.start()
        self.fake.tasks.update({
            "86lead": {"id": "86lead", "url": "https://app.clickup.com/t/86lead",
                       "list": {"id": "900200"}, "locations": [], "attachments": []},
            # Home List not allowed; allowed list only as an additional location.
            "86extra": {"id": "86extra", "url": "https://app.clickup.com/t/86extra",
                        "list": {"id": "555"}, "locations": [{"id": "900200"}, {"id": "900100"}],
                        "attachments": []},
        })

    def tearDown(self):
        self.env2.stop()
        super().tearDown()

    def test_second_home_list_allowed(self):
        r = self.run_upload(task_id="86lead")
        self.assertTrue(r["upload_ok"] and r["upload_verified"])
        self.assertEqual(r["list_id"], "900200")

    def test_first_home_list_still_allowed(self):
        r = self.run_upload(task_id="86ok")
        self.assertTrue(r["upload_ok"] and r["upload_verified"])
        self.assertEqual(r["list_id"], "900100")

    def test_other_home_list_refused(self):
        self.assert_refused_early(lambda: self.run_upload(task_id="86other"))

    def test_allowed_list_only_as_extra_location_refused(self):
        self.assert_refused_early(lambda: self.run_upload(task_id="86extra"))

    def test_missing_or_wrong_key_refused(self):
        self.assert_refused_early(lambda: self.run_upload(task_id="86lead", headers={}))
        self.assert_refused_early(lambda: self.run_upload(
            task_id="86lead", headers={cu.HEADER_NAME: "Bearer " + KEY}))
        self.assertEqual(self.fake.calls, [])

    def test_repeat_on_second_list_no_duplicate(self):
        first = self.run_upload(task_id="86lead")
        second = self.run_upload(task_id="86lead")
        self.assertEqual(self.fake.uploads, 1)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["attachment_id"], first["attachment_id"])


class GateTest(unittest.TestCase):
    def test_disabled_without_full_config(self):
        for drop in ENV:
            env = {k: v for k, v in ENV.items() if k != drop}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertFalse(cu.upload_gate()[0], drop)
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertTrue(cu.upload_gate()[0])
        with mock.patch.dict(os.environ, {**ENV, "SCREENSHOT_UPLOAD_KEY": "short"}, clear=True):
            self.assertFalse(cu.upload_gate()[0])


if __name__ == "__main__":
    unittest.main()
