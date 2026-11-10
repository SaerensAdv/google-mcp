"""Unit tests for the Google Places tools (mocked HTTP, temp usage file)."""

import json
import os
import tempfile
import unittest
from unittest import mock

import httpx

from places_mcp import tools as places
from places_mcp import usage


def _place(pid, name, lat, lng, rating=4.5, count=100, website="https://x.be", status="OPERATIONAL"):
    return {
        "id": pid, "displayName": {"text": name}, "formattedAddress": f"{name} straat 1, Gent",
        "location": {"latitude": lat, "longitude": lng}, "primaryTypeDisplayName": {"text": "Tandarts"},
        "googleMapsUri": f"https://maps.google.com/?cid={pid}", "businessStatus": status,
        "rating": rating, "userRatingCount": count, "websiteUri": website,
    }


class FakeHttp:
    """Patches httpx.Client so each request returns the next queued (status, json)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        status, payload = self.responses.pop(0)
        return httpx.Response(status, json=payload)

    def patch(self):
        transport = httpx.MockTransport(self.handler)
        real = httpx.Client
        return mock.patch.object(places.httpx, "Client", lambda **kw: real(transport=transport, **kw))


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "GOOGLE_API_KEY": "secret-key-123",
            "PLACES_USAGE_FILE": os.path.join(self.tmp.name, "usage.json"),
            "PLACES_DAILY_CAP": "5",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class UsageTests(BaseCase):
    def test_cap_enforced_and_persisted(self):
        for i in range(5):
            self.assertEqual(usage.reserve_call(), (i + 1, 5))
        with self.assertRaises(usage.DailyCapReached) as cm:
            usage.reserve_call()
        self.assertIn("5/5", str(cm.exception))
        self.assertEqual(usage.usage_today(), (5, 5))

    def test_old_days_reset(self):
        with open(os.environ["PLACES_USAGE_FILE"], "w") as f:
            json.dump({"2000-01-01": 999}, f)
        self.assertEqual(usage.reserve_call()[0], 1)

    def test_cap_config(self):
        os.environ["PLACES_DAILY_CAP"] = "0"
        with self.assertRaises(usage.DailyCapReached):
            usage.reserve_call()
        os.environ["PLACES_DAILY_CAP"] = "abc"
        self.assertEqual(usage.daily_cap(), usage.DEFAULT_DAILY_CAP)
        del os.environ["PLACES_DAILY_CAP"]
        self.assertEqual(usage.daily_cap(), 200)


class SearchTests(BaseCase):
    def test_basic_search(self):
        fake = FakeHttp([(200, {"places": [
            _place("ChIJaaaaaaaaaa", "A", 51.05, 3.72, 4.0, 10),
            _place("ChIJbbbbbbbbbb", "B", 51.06, 3.73, 5.0, 30, website=None, status="CLOSED_TEMPORARILY"),
        ]})])
        with fake.patch():
            out = places.search_places("tandarts  Gent")
        req = fake.requests[0]
        self.assertEqual(req.headers["X-Goog-Api-Key"], "secret-key-123")
        self.assertNotIn("secret-key-123", str(req.url))
        self.assertEqual(req.headers["X-Goog-FieldMask"], places.SEARCH_FIELDS)
        self.assertNotIn("reviews", req.headers["X-Goog-FieldMask"])
        body = json.loads(req.content)
        self.assertEqual(body, {"textQuery": "tandarts Gent", "pageSize": 20, "languageCode": "nl", "regionCode": "BE"})
        self.assertEqual(out["count"], 2)
        a, b = out["results"]
        self.assertEqual((a["position"], a["name"], a["rating"], a["reviews"], a["category"]), (1, "A", 4.0, 10, "Tandarts"))
        self.assertNotIn("business_status", a)
        self.assertEqual(b["business_status"], "CLOSED_TEMPORARILY")
        self.assertEqual(out["summary"], {"avg_rating": 4.5, "median_reviews": 20.0, "with_website": 1})
        self.assertEqual(usage.usage_today()[0], 1)

    def test_near_place_name_and_strict_radius(self):
        fake = FakeHttp([
            (200, {"places": [{"location": {"latitude": 51.0543, "longitude": 3.7174}, "formattedAddress": "Gent, België"}]}),
            (200, {"places": [
                _place("ChIJnear000000", "Near", 51.055, 3.72),
                _place("ChIJfar0000000", "Far", 50.85, 4.35),  # Brussels
            ]}),
        ])
        with fake.patch():
            out = places.search_places("kapper", near="Gent", radius_km=5, strict_radius=True)
        self.assertEqual(json.loads(fake.requests[0].content)["pageSize"], 1)
        self.assertEqual(fake.requests[0].headers["X-Goog-FieldMask"], places.GEOCODE_FIELDS)
        bias = json.loads(fake.requests[1].content)["locationBias"]["circle"]
        self.assertEqual(bias["radius"], 5000)
        self.assertEqual([r["name"] for r in out["results"]], ["Near"])
        self.assertEqual(out["outside_radius_removed"], 1)
        self.assertEqual(out["near"], "Gent, België")
        self.assertLess(out["results"][0]["distance_km"], 1)
        self.assertEqual(usage.usage_today()[0], 2)

    def test_near_coordinates_no_extra_call(self):
        fake = FakeHttp([(200, {"places": []})])
        with fake.patch():
            out = places.search_places("bakker", near="51.05, 3.72")
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(json.loads(fake.requests[0].content)["locationBias"]["circle"]["radius"], 10000)
        self.assertEqual(out["count"], 0)
        self.assertIsNone(out["summary"]["avg_rating"])

    def test_input_validation(self):
        for kwargs in ({"query": ""}, {"query": "x", "radius_km": 80, "near": "1,1"},
                       {"query": "x", "radius_km": 5}, {"query": "x", "near": "100,1"}):
            with self.assertRaises(places.PlacesError):
                places.search_places(**kwargs)
        self.assertEqual(usage.usage_today()[0], 0)

    def test_cap_blocks_before_http(self):
        os.environ["PLACES_DAILY_CAP"] = "0"
        fake = FakeHttp([])
        with fake.patch():
            with self.assertRaises(usage.DailyCapReached):
                places.search_places("x")
        self.assertEqual(fake.requests, [])


class DetailsTests(BaseCase):
    def test_details_with_reviews(self):
        p = _place("ChIJdetail0000", "D", 51.0, 3.7)
        p.update({"types": ["dentist"], "nationalPhoneNumber": "09 123 45 67",
                  "regularOpeningHours": {"weekdayDescriptions": ["maandag: 09:00–17:00"]},
                  "reviews": [{"rating": 5, "relativePublishTimeDescription": "een week geleden",
                               "publishTime": "2026-11-01T10:00:00Z", "authorAttribution": {"displayName": "Jan"},
                               "text": {"text": "Top"}}] * 6})
        fake = FakeHttp([(200, p)])
        with fake.patch():
            out = places.place_details("places/ChIJdetail0000")
        req = fake.requests[0]
        self.assertTrue(str(req.url).startswith("https://places.googleapis.com/v1/places/ChIJdetail0000"))
        self.assertIn("reviews", req.headers["X-Goog-FieldMask"])
        self.assertEqual(len(out["latest_reviews"]), 5)
        self.assertEqual(out["latest_reviews"][0], {"rating": 5, "when": "een week geleden",
                                                    "published": "2026-11-01T10:00:00Z", "author": "Jan", "text": "Top"})
        self.assertEqual(out["phone"], "09 123 45 67")
        self.assertIn("at most 5", out["reviews_note"])

    def test_details_no_reviews_returned(self):
        fake = FakeHttp([(200, _place("ChIJdetail0000", "D", 51.0, 3.7))])
        with fake.patch():
            out = places.place_details("ChIJdetail0000")
        self.assertEqual(out["latest_reviews"], [])
        self.assertIn("no review texts", out["reviews_note"])

    def test_details_without_reviews(self):
        fake = FakeHttp([(200, _place("ChIJdetail0000", "D", 51.0, 3.7))])
        with fake.patch():
            out = places.place_details("ChIJdetail0000", include_reviews=False)
        self.assertNotIn("reviews", fake.requests[0].headers["X-Goog-FieldMask"])
        self.assertNotIn("latest_reviews", out)

    def test_bad_place_id(self):
        with self.assertRaises(places.PlacesError):
            places.place_details("../x")


class ErrorTests(BaseCase):
    def _err(self, status, payload):
        fake = FakeHttp([(status, payload)])
        with fake.patch():
            with self.assertRaises(places.PlacesError) as cm:
                places.search_places("x")
        return str(cm.exception)

    def test_disabled(self):
        msg = self._err(403, {"error": {"message": "Places API (New) has not been used in project 1",
                                        "details": [{"reason": "SERVICE_DISABLED"}]}})
        self.assertIn("not enabled", msg)

    def test_key_restricted(self):
        msg = self._err(403, {"error": {"message": "Requests to this API are blocked.",
                                        "details": [{"reason": "API_KEY_SERVICE_BLOCKED"}]}})
        self.assertIn("restricted", msg)

    def test_billing_and_other(self):
        self.assertIn("billing", self._err(403, {"error": {"message": "Billing must be enabled"}}))
        self.assertIn("rate limit", self._err(429, {"error": {"message": "quota"}}))
        self.assertNotIn("secret-key-123", self._err(400, {"error": {"message": "bad request"}}))

    def test_missing_key(self):
        del os.environ["GOOGLE_API_KEY"]
        with self.assertRaises(places.PlacesError):
            places.search_places("x")

    def test_tool_error_wrapping(self):
        from fastmcp.exceptions import ToolError

        os.environ["PLACES_DAILY_CAP"] = "0"
        with self.assertRaisesRegex(ToolError, "Daily Places limit"):
            places._run(places.search_places, "x")


class RegistrationTests(unittest.TestCase):
    def test_registered_read_only(self):
        import asyncio
        from ads_mcp import server

        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in ("places_search", "places_details"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name].annotations.readOnlyHint)


if __name__ == "__main__":
    unittest.main()
