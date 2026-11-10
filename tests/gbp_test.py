"""Unit tests for the Google Business Profile tools (mocked Google responses)."""

import asyncio
import json
import unittest
from datetime import date
from unittest import mock

from ads_mcp.google_rest import GoogleApiError
from gbp_mcp import auth
from gbp_mcp import tools as gbp


class FakeGbp:
    """Replaces gbp_mcp.auth.get_json: maps (service, path suffix) -> response or list of pages."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, service, path, params=None):
        self.calls.append((service, path, dict(params or {})))
        for (svc, suffix), resp in self.routes.items():
            if svc == service and path.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                if isinstance(resp, list):
                    idx = sum(1 for c in self.calls if c[0] == svc and c[1].endswith(suffix)) - 1
                    return resp[min(idx, len(resp) - 1)]
                return resp
        raise AssertionError(f"unexpected call {service} {path}")


def patch_routes(routes):
    fake = FakeGbp(routes)
    return fake, mock.patch.object(auth, "get_json", fake)


# Real payloads captured from Google for project 294994430690 (2026-11-10).
SERVICE_DISABLED = {"error": {
    "code": 403,
    "message": "My Business Account Management API has not been used in project 294994430690 before or it is "
               "disabled. Enable it by visiting https://console.developers.google.com/apis/api/"
               "mybusinessaccountmanagement.googleapis.com/overview?project=294994430690 then retry.",
    "status": "PERMISSION_DENIED",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                 "domain": "googleapis.com",
                 "metadata": {"activationUrl": "https://console.developers.google.com/apis/api/"
                                               "mybusinessaccountmanagement.googleapis.com/overview?project=294994430690",
                              "service": "mybusinessaccountmanagement.googleapis.com",
                              "consumer": "projects/294994430690",
                              "serviceTitle": "My Business Account Management API"}}],
}}
QUOTA_ZERO = {"error": {
    "code": 429,
    "message": "Quota exceeded for quota metric 'Requests' and limit 'Requests per minute' of service "
               "'businessprofileperformance.googleapis.com' for consumer 'project_number:294994430690'.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED",
                 "domain": "googleapis.com",
                 "metadata": {"consumer": "projects/294994430690",
                              "service": "businessprofileperformance.googleapis.com",
                              "quota_limit_value": "0", "quota_limit": "DefaultRequestsPerMinutePerProject"}},
                {"@type": "type.googleapis.com/google.rpc.Help", "links": [{"url": "https://cloud.google.com/x"}]}],
}}
NO_PERMISSION = {"error": {"code": 403, "message": "The caller does not have permission",
                           "status": "PERMISSION_DENIED"}}


class TranslateErrorTests(unittest.TestCase):
    def test_service_disabled(self):
        e = auth.translate_gbp_error("accounts", 403, SERVICE_DISABLED)
        self.assertEqual(e.reason, "SERVICE_DISABLED")
        msg = str(e)
        self.assertIn("switched off", msg)
        self.assertIn("294994430690", msg)
        self.assertIn("mybusinessaccountmanagement.googleapis.com/overview?project=294994430690", msg)
        for api in ("Account Management", "Business Information", "Performance", "Google My Business"):
            self.assertIn(api, msg)
        self.assertNotIn("PERMISSION_DENIED", msg)

    def test_quota_zero_means_access_not_approved(self):
        e = auth.translate_gbp_error("performance", 429, QUOTA_ZERO)
        self.assertEqual(e.reason, "ACCESS_NOT_APPROVED")
        self.assertIn("not (yet) approved", str(e))
        self.assertIn(auth.ACCESS_REQUEST_URL, str(e))
        self.assertIn("294994430690", str(e))

    def test_normal_rate_limit(self):
        payload = json.loads(json.dumps(QUOTA_ZERO))
        payload["error"]["details"][0]["metadata"]["quota_limit_value"] = "300"
        e = auth.translate_gbp_error("performance", 429, payload)
        self.assertEqual(e.reason, "RATE_LIMIT")
        self.assertIn("wait a minute", str(e))

    def test_no_permission_on_location(self):
        e = auth.translate_gbp_error("info", 403, NO_PERMISSION)
        self.assertEqual(e.reason, "PERMISSION_DENIED")
        self.assertIn("no access to this account or location", str(e))

    def test_scope_falls_through_to_shared_translation(self):
        e = auth.translate_gbp_error(
            "info", 403, {"error": {"message": "Request had insufficient authentication scopes."}})
        self.assertEqual(e.reason, "SCOPE")

    def test_not_found(self):
        e = auth.translate_gbp_error("info", 404, {"error": {"message": "Requested entity was not found."}})
        self.assertEqual(e.reason, "NOT_FOUND")

    def test_get_json_only_uses_get_and_translates(self):
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=403, json=lambda: SERVICE_DISABLED)
        with mock.patch.object(auth, "_session", return_value=session):
            with self.assertRaises(GoogleApiError) as cm:
                auth.get_json("accounts", "accounts", {"pageSize": 20, "pageToken": None})
        self.assertEqual(cm.exception.reason, "SERVICE_DISABLED")
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://mybusinessaccountmanagement.googleapis.com/v1/accounts")
        self.assertEqual(session.get.call_args.kwargs["params"], {"pageSize": 20})
        for method in ("post", "put", "patch", "delete"):
            getattr(session, method).assert_not_called()

    def test_module_has_no_write_helpers(self):
        for name in ("post_json", "patch_json", "delete_json", "put_json"):
            self.assertFalse(hasattr(auth, name))

    def test_missing_scope_becomes_api_error(self):
        from ads_mcp.user_credentials import MissingGooglePermission
        with mock.patch.object(auth, "_session", side_effect=MissingGooglePermission("no business.manage")):
            with self.assertRaises(GoogleApiError) as cm:
                auth.get_json("accounts", "accounts")
        self.assertEqual(cm.exception.reason, "SCOPE")


class InputTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(gbp.clean_account_id("accounts/123"), "123")
        self.assertEqual(gbp.clean_location_id("locations/456"), ("456", None))
        self.assertEqual(gbp.clean_location_id("accounts/9/locations/456"), ("456", "9"))
        for bad in ("", "abc", "accounts/x"):
            with self.assertRaises(gbp.GbpInputError):
                gbp.clean_account_id(bad)
        with self.assertRaises(gbp.GbpInputError):
            gbp.clean_location_id("locations/../1")


class ToolLogicTests(unittest.TestCase):
    def test_list_accounts_paged(self):
        fake, p = patch_routes({("accounts", "accounts"): [
            {"accounts": [{"name": "accounts/1", "accountName": "Axel", "type": "PERSONAL", "role": "PRIMARY_OWNER"}],
             "nextPageToken": "t2"},
            {"accounts": [{"name": "accounts/2", "accountName": "Clients", "type": "LOCATION_GROUP",
                           "role": "MANAGER", "verificationState": "VERIFIED"}]},
        ]})
        with p:
            out = gbp.list_accounts()
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["accounts"][0], {"account_id": "1", "name": "Axel", "type": "PERSONAL",
                                              "role": "PRIMARY_OWNER", "verification_state": None})
        self.assertEqual(fake.calls[1][2]["pageToken"], "t2")

    def test_list_locations(self):
        fake, p = patch_routes({("info", "accounts/2/locations"): {
            "locations": [{
                "name": "locations/77", "title": "Bakkerij Jan", "storeCode": "B1",
                "storefrontAddress": {"addressLines": ["Kerkstraat 1"], "postalCode": "9000",
                                      "locality": "Gent", "regionCode": "BE"},
                "phoneNumbers": {"primaryPhone": "09 123 45 67"}, "websiteUri": "https://jan.be",
                "categories": {"primaryCategory": {"displayName": "Bakkerij"}},
                "openInfo": {"status": "OPEN"},
                "metadata": {"placeId": "ChIJ1", "mapsUri": "https://maps.google.com/?cid=1"},
            }],
            "nextPageToken": "n", "totalSize": 3,
        }})
        with p:
            out = gbp.list_locations("accounts/2", limit=500)
        loc = out["locations"][0]
        self.assertEqual(loc["location_id"], "77")
        self.assertEqual(loc["address"], "Kerkstraat 1, 9000 Gent, BE")
        self.assertEqual(loc["primary_category"], "Bakkerij")
        self.assertEqual(out["next_page_token"], "n")
        params = fake.calls[0][2]
        self.assertEqual(params["pageSize"], 100)
        self.assertIn("readMask", params)

    def test_location_details(self):
        _, p = patch_routes({("info", "locations/77"): {
            "name": "locations/77", "title": "Bakkerij Jan", "languageCode": "nl",
            "categories": {"primaryCategory": {"displayName": "Bakkerij"},
                           "additionalCategories": [{"displayName": "Patisserie"}]},
            "regularHours": {"periods": [{"openDay": "MONDAY", "openTime": {"hours": 7},
                                          "closeDay": "MONDAY", "closeTime": {"hours": 18, "minutes": 30}}]},
            "specialHours": {"specialHourPeriods": [
                {"startDate": {"year": 2026, "month": 12, "day": 25}, "closed": True}]},
            "profile": {"description": "Ambachtelijk brood"},
            "openInfo": {"status": "OPEN", "openingDate": {"year": 1999, "month": 5}},
            "metadata": {"newReviewUri": "https://g.page/r/x/review", "hasVoiceOfMerchant": True},
            "latlng": {"latitude": 51.05, "longitude": 3.72},
        }})
        with p:
            out = gbp.location_details("77")
        self.assertEqual(out["additional_categories"], ["Patisserie"])
        self.assertEqual(out["regular_hours"][0]["close"], "18:30")
        self.assertEqual(out["special_hours"], [{"date": "2026-12-25", "closed": True}])
        self.assertEqual(out["opening_date"], "1999-05-01")
        self.assertTrue(out["has_voice_of_merchant"])
        self.assertEqual(out["latlng"], {"lat": 51.05, "lng": 3.72})

    def test_performance_totals_and_defaults(self):
        def series(metric, values):
            return {"dailyMetric": metric, "timeSeries": {"datedValues": [
                {"date": {"year": 2026, "month": 11, "day": d}, **({"value": str(v)} if v else {})}
                for d, v in values]}}
        _, p = patch_routes({("performance", "locations/77:fetchMultiDailyMetricsTimeSeries"): {
            "multiDailyMetricTimeSeries": [{"dailyMetricTimeSeries": [
                series("BUSINESS_IMPRESSIONS_MOBILE_SEARCH", [(1, 10), (2, 5)]),
                series("BUSINESS_IMPRESSIONS_DESKTOP_MAPS", [(1, 3), (2, 0)]),
                series("CALL_CLICKS", [(1, 2)]),
                series("WEBSITE_CLICKS", [(2, 4)]),
                series("BUSINESS_DIRECTION_REQUESTS", [(1, 1)]),
            ]}]}})
        with p, mock.patch.object(gbp, "_today", return_value=date(2026, 11, 10)):
            out = gbp.performance("77", include_daily=True)
            fake = auth.get_json
        self.assertEqual(out["date_range"], {"start": "2026-10-13", "end": "2026-11-09", "days": 28})
        s = out["summary"]
        self.assertEqual((s["profile_views"], s["views_google_search"], s["views_google_maps"]), (18, 15, 3))
        self.assertEqual((s["calls"], s["website_clicks"], s["direction_requests"]), (2, 4, 1))
        self.assertEqual(out["daily"][0]["date"], "2026-11-01")
        self.assertEqual(out["daily"][1]["views_search_mobile"], 5)
        params = fake.calls[0][2]
        self.assertEqual(len(params["dailyMetrics"]), len(gbp.DAILY_METRICS))
        self.assertEqual(params["dailyRange.startDate.day"], 13)

    def test_performance_input_validation(self):
        with self.assertRaises(gbp.GbpInputError):
            gbp.performance("77", start_date="2026-11-05", end_date="2026-11-01")
        with self.assertRaises(gbp.GbpInputError):
            gbp.performance("77", start_date="05/11/2026")
        with self.assertRaises(gbp.GbpInputError):
            gbp.performance("77", start_date="2020-01-01", end_date="2026-01-01")

    def test_search_keywords(self):
        fake, p = patch_routes({("performance", "searchkeywords/impressions/monthly"): [
            {"searchKeywordsCounts": [
                {"searchKeyword": "bakker gent", "insightsValue": {"value": "120"}},
                {"searchKeyword": "brood", "insightsValue": {"threshold": "15"}}],
             "nextPageToken": "p2"},
            {"searchKeywordsCounts": [{"searchKeyword": "bakkerij jan", "insightsValue": {"value": "300"}}]},
        ]})
        with p, mock.patch.object(gbp, "_today", return_value=date(2026, 11, 10)):
            out = gbp.search_keywords("77")
        self.assertEqual(out["month_range"], {"start": "2026-08", "end": "2026-10"})
        self.assertEqual([k["keyword"] for k in out["keywords"]], ["bakkerij jan", "bakker gent", "brood"])
        self.assertTrue(out["keywords"][2]["below_threshold"])
        self.assertIsNone(out["next_page_token"])
        self.assertEqual(fake.calls[1][2]["pageToken"], "p2")
        self.assertEqual(fake.calls[0][2]["monthlyRange.startMonth.month"], 8)

    def test_search_keywords_january_default(self):
        _, p = patch_routes({("performance", "monthly"): {}})
        with p, mock.patch.object(gbp, "_today", return_value=date(2027, 1, 15)):
            out = gbp.search_keywords("77")
        self.assertEqual(out["month_range"], {"start": "2026-10", "end": "2026-12"})

    def test_reviews_summary(self):
        page = {
            "averageRating": 4.333, "totalReviewCount": 87, "nextPageToken": "r2",
            "reviews": [
                {"reviewId": "a", "starRating": "FIVE", "comment": "Top!", "createTime": "2026-11-01T10:00:00Z",
                 "reviewer": {"displayName": "Els"}, "reviewReply": {"comment": "Dank!", "updateTime": "x"}},
                {"reviewId": "b", "starRating": "TWO", "comment": "x" * 2000,
                 "reviewer": {"isAnonymous": True, "displayName": "A Google user"}},
            ],
        }
        fake, p = patch_routes({("reviews", "accounts/2/locations/77/reviews"): page})
        with p:
            out = gbp.list_reviews("77", account_id="2", limit=2, order="lowest")
        rs = out["rating_summary"]
        self.assertEqual((rs["average_rating"], rs["total_reviews"], rs["returned"]), (4.33, 87, 2))
        self.assertEqual(rs["distribution_of_returned"], {"5": 1, "4": 0, "3": 0, "2": 1, "1": 0})
        self.assertEqual(rs["returned_without_reply"], 1)
        self.assertEqual(out["reviews"][1]["reviewer"], "Anonymous")
        self.assertEqual(len(out["reviews"][1]["comment"]), gbp.COMMENT_MAX_CHARS + 1)
        self.assertEqual(out["next_page_token"], "r2")
        self.assertEqual(fake.calls[0][2]["orderBy"], "rating")
        self.assertEqual(len(fake.calls), 1)

    def test_reviews_account_from_location_name(self):
        _, p = patch_routes({("reviews", "accounts/9/locations/77/reviews"): {}})
        with p:
            out = gbp.list_reviews("accounts/9/locations/77")
        self.assertEqual(out["rating_summary"]["total_reviews"], 0)

    def test_reviews_needs_account(self):
        with self.assertRaises(gbp.GbpInputError):
            gbp.list_reviews("77")
        with self.assertRaises(gbp.GbpInputError):
            gbp.list_reviews("77", account_id="2", order="random")


class ToolWrappingTests(unittest.TestCase):
    def test_google_error_becomes_single_clear_message(self):
        from fastmcp.exceptions import ToolError
        err = auth.translate_gbp_error("performance", 429, QUOTA_ZERO)
        _, p = patch_routes({("performance", "fetchMultiDailyMetricsTimeSeries"): err})
        with p:
            with self.assertRaises(ToolError) as cm:
                gbp._run(gbp.performance, "77")
        self.assertIn("not (yet) approved", str(cm.exception))
        self.assertNotIn("RESOURCE_EXHAUSTED", str(cm.exception))

    def test_tools_registered_read_only(self):
        from ads_mcp import server

        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in ("gbp_list_accounts", "gbp_list_locations", "gbp_location_details",
                     "gbp_performance", "gbp_search_keywords", "gbp_list_reviews"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name].annotations.readOnlyHint)


if __name__ == "__main__":
    unittest.main()
