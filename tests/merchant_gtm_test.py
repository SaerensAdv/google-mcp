"""Unit tests for shared Google login, Merchant Center and Tag Manager tools (mocked)."""

import json
import os
import unittest
from unittest import mock

from google.auth.exceptions import RefreshError

from ads_mcp import google_rest, user_credentials
from gtm_mcp import tools as gtm
from merchant_mcp import tools as merchant


class FakeRoutes:
    """Replaces google_rest.get_json: maps URL suffix -> response (or list of pages)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, api_label, url, scopes, params=None):
        self.calls.append((url, dict(params or {}), tuple(scopes)))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                if isinstance(resp, list):
                    idx = sum(1 for c in self.calls if c[0].endswith(suffix)) - 1
                    return resp[min(idx, len(resp) - 1)]
                return resp
        raise AssertionError(f"unexpected url {url}")


# ---------------------------------------------------------------------------
# Shared credentials
# ---------------------------------------------------------------------------


class UserCredentialsTests(unittest.TestCase):
    def setUp(self):
        user_credentials.clear_cache()
        self.env = mock.patch.dict(os.environ, {
            "GSC_CLIENT_ID": "cid", "GSC_CLIENT_SECRET": "sec",
            "GSC_REFRESH_TOKEN": "old", "GOOGLE_REFRESH_TOKEN": "new",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        user_credentials.clear_cache()

    def _patch_refresh(self, behaviour):
        def refresh(creds_self, request):
            outcome = behaviour(creds_self.refresh_token, tuple(creds_self.scopes))
            if outcome:
                raise RefreshError(outcome)
            creds_self.token = "tok-" + creds_self.refresh_token
        return mock.patch("google.oauth2.credentials.Credentials.refresh", refresh)

    def test_new_token_preferred(self):
        with self._patch_refresh(lambda rt, sc: None):
            c = user_credentials.get_user_credentials(["s1"])
        self.assertEqual(c.refresh_token, "new")

    def test_falls_back_to_gsc_token_when_new_is_broken(self):
        with self._patch_refresh(lambda rt, sc: "invalid_grant" if rt == "new" else None):
            c = user_credentials.get_user_credentials(["s1"])
        self.assertEqual(c.refresh_token, "old")

    def test_falls_back_when_new_token_not_set(self):
        del os.environ["GOOGLE_REFRESH_TOKEN"]
        with self._patch_refresh(lambda rt, sc: None):
            c = user_credentials.get_user_credentials(["s1"])
        self.assertEqual(c.refresh_token, "old")

    def test_missing_scope_message(self):
        with self._patch_refresh(lambda rt, sc: "invalid_scope: bad"):
            with self.assertRaises(user_credentials.MissingGooglePermission) as cm:
                user_credentials.get_user_credentials(["https://www.googleapis.com/auth/content"])
        msg = str(cm.exception)
        self.assertIn("content", msg)
        self.assertIn("GOOGLE_REFRESH_TOKEN", msg)
        self.assertIn("invalid_scope", msg)
        self.assertNotIn("new", msg.replace("new refresh", ""))  # token values never echoed

    def test_cached_per_scope(self):
        calls = []
        with self._patch_refresh(lambda rt, sc: calls.append(sc)):
            user_credentials.get_user_credentials(["a"])
            user_credentials.get_user_credentials(["a"])
            user_credentials.get_user_credentials(["b"])
        self.assertEqual(len(calls), 2)

    def test_extra_token_env_first(self):
        os.environ["GA4_REFRESH_TOKEN"] = "ga4"
        try:
            with self._patch_refresh(lambda rt, sc: None):
                c = user_credentials.get_user_credentials(["s"], extra_token_env=("GA4_REFRESH_TOKEN",))
            self.assertEqual(c.refresh_token, "ga4")
        finally:
            del os.environ["GA4_REFRESH_TOKEN"]


class TranslateErrorTests(unittest.TestCase):
    def test_service_disabled(self):
        payload = {"error": {"message": "Merchant API has not been used in project 12 before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/merchantapi.googleapis.com/overview?project=12 then retry.",
                             "details": [{"reason": "SERVICE_DISABLED", "metadata": {"activationUrl": "https://console.developers.google.com/apis/api/merchantapi.googleapis.com/overview?project=12"}}]}}
        e = google_rest.translate_error("Merchant API", 403, payload)
        self.assertEqual(e.reason, "SERVICE_DISABLED")
        self.assertIn("not enabled", str(e))
        self.assertIn("project=12", str(e))

    def test_scope(self):
        e = google_rest.translate_error("Tag Manager API", 403, {"error": {"message": "Request had insufficient authentication scopes."}})
        self.assertEqual(e.reason, "SCOPE")

    def test_permission_and_rate(self):
        self.assertIn("access denied", str(google_rest.translate_error("X", 403, {"error": {"message": "nope"}})))
        self.assertEqual(google_rest.translate_error("X", 429, {}).reason, "RATE_LIMIT")

    def test_missing_permission_becomes_api_error(self):
        with mock.patch.object(google_rest, "get_user_credentials",
                               side_effect=user_credentials.MissingGooglePermission("no content scope")):
            with self.assertRaises(google_rest.GoogleApiError) as cm:
                google_rest.get_json("Merchant API", "https://x", ["s"])
        self.assertIn("no content scope", str(cm.exception))


# ---------------------------------------------------------------------------
# Merchant Center
# ---------------------------------------------------------------------------


def _product(offer, disapproved_ctry=None, issues=()):
    dest = {"reportingContext": "SHOPPING_ADS", "approvedCountries": ["NL"]}
    if disapproved_ctry:
        dest["disapprovedCountries"] = [disapproved_ctry]
    return {
        "offerId": offer, "feedLabel": "BE", "contentLanguage": "nl",
        "productAttributes": {"title": f"T {offer}", "price": {"amountMicros": "19990000", "currencyCode": "EUR"},
                              "availability": "IN_STOCK", "link": f"https://x/{offer}"},
        "productStatus": {"destinationStatuses": [dest], "itemLevelIssues": list(issues)},
    }


class MerchantTests(unittest.TestCase):
    def test_account_id_validation(self):
        self.assertEqual(merchant.clean_account_id("accounts/123"), "123")
        with self.assertRaises(merchant.MerchantInputError):
            merchant.clean_account_id("abc")

    def test_list_accounts_and_subaccounts(self):
        fake = FakeRoutes({
            "accounts/v1/accounts": {"accounts": [{"accountId": "2", "accountName": "b"}, {"accountId": "1", "accountName": "A", "timeZone": {"id": "Europe/Brussels"}}]},
            ":listSubaccounts": {"accounts": [{"name": "accounts/9", "accountName": "Sub"}]},
        })
        with mock.patch.object(google_rest, "get_json", fake):
            out = merchant.list_accounts()
            self.assertEqual([a["account_id"] for a in out["accounts"]], ["1", "2"])
            self.assertEqual(out["accounts"][0]["time_zone"], "Europe/Brussels")
            sub = merchant.list_accounts("5")
            self.assertEqual(sub["accounts"][0]["account_id"], "9")
        self.assertTrue(fake.calls[-1][0].endswith("accounts/5:listSubaccounts"))
        self.assertEqual(fake.calls[0][2], ("https://www.googleapis.com/auth/content",))

    def test_account_issues_sorted(self):
        fake = FakeRoutes({"/issues": {"accountIssues": [
            {"title": "tip", "severity": "SUGGESTION"},
            {"title": "suspended", "severity": "CRITICAL", "impactedDestinations": [
                {"reportingContext": "SHOPPING_ADS", "impacts": [{"regionCode": "BE"}, {"regionCode": "NL"}]}]},
        ]}})
        with mock.patch.object(google_rest, "get_json", fake):
            out = merchant.account_issues("1")
        self.assertEqual(out["issues"][0]["title"], "suspended")
        self.assertEqual(out["issues"][0]["impacted"][0]["regions"], ["BE", "NL"])
        self.assertEqual(out["by_severity"], {"SUGGESTION": 1, "CRITICAL": 1})
        self.assertEqual(fake.calls[0][1]["languageCode"], "nl")

    def test_status_summary(self):
        fake = FakeRoutes({"aggregateProductStatuses": {"aggregateProductStatuses": [
            {"reportingContext": "FREE_LISTINGS", "country": "BE", "stats": {"activeCount": "10"}},
            {"reportingContext": "SHOPPING_ADS", "country": "BE",
             "stats": {"activeCount": "80", "disapprovedCount": "20"},
             "itemLevelIssues": [
                 {"severity": "DEMOTED", "productCount": "50", "description": "Missing GTIN"},
                 {"severity": "DISAPPROVED", "productCount": "20", "description": "Price mismatch"}]},
        ]}})
        with mock.patch.object(google_rest, "get_json", fake):
            out = merchant.product_status_summary("1", country="be", reporting_context="shopping_ads")
        g = out["groups"][0]
        self.assertEqual((g["reporting_context"], g["disapproved"], g["active"]), ("SHOPPING_ADS", 20, 80))
        self.assertEqual(g["issues"][0]["description"], "Price mismatch")
        self.assertEqual(out["totals"]["disapproved"], 20)
        self.assertEqual(fake.calls[0][1]["filter"], 'country = "BE" AND reporting_context = "SHOPPING_ADS"')

    def test_list_products_all_paged(self):
        fake = FakeRoutes({"/products": {"products": [_product("a")], "nextPageToken": "t2"}})
        with mock.patch.object(google_rest, "get_json", fake):
            out = merchant.list_products("1", limit=5)
        p = out["products"][0]
        self.assertEqual(p["price"], "19.99 EUR")
        self.assertFalse(p["disapproved"])
        self.assertEqual(out["next_page_token"], "t2")
        self.assertEqual(fake.calls[0][1]["pageSize"], 5)

    def test_list_products_disapproved_scans_pages(self):
        issue = {"severity": "DISAPPROVED", "description": "Image too small", "attribute": "image_link"}
        demote = {"severity": "DEMOTED", "description": "Missing GTIN"}
        pages = [
            {"products": [_product("a"), _product("b", "BE", [issue, demote])], "nextPageToken": "p2"},
            {"products": [_product("c", "BE", [issue]), _product("d", None, [demote])]},
        ]
        with mock.patch.object(google_rest, "get_json", FakeRoutes({"/products": pages})):
            out = merchant.list_products("1", status="disapproved")
        with mock.patch.object(google_rest, "get_json", FakeRoutes({"/products": pages})):
            issues_out = merchant.list_products("1", status="issues")
        self.assertEqual([p["offer_id"] for p in out["products"]], ["b", "c"])
        self.assertEqual(out["products_scanned"], 4)
        self.assertEqual(out["top_reasons"], [{"reason": "Image too small", "products": 2}])
        self.assertEqual(issues_out["matched"], 3)

    def test_bad_status(self):
        with self.assertRaises(merchant.MerchantInputError):
            merchant.list_products("1", status="weird")

    def test_tool_error_wrapping(self):
        from fastmcp.exceptions import ToolError

        def boom(*a, **k):
            raise google_rest.GoogleApiError("The Merchant API is not enabled")

        with mock.patch.object(google_rest, "get_json", boom):
            with self.assertRaisesRegex(ToolError, "not enabled"):
                merchant._run(merchant.list_accounts)


# ---------------------------------------------------------------------------
# Tag Manager
# ---------------------------------------------------------------------------


LIVE_VERSION = {
    "containerVersionId": "42", "name": "v42",
    "container": {"publicId": "GTM-ABC123", "name": "Site"},
    "customTemplate": [{"containerId": "7", "templateId": "99", "name": "Facebook Pixel"}],
    "trigger": [
        {"triggerId": "10", "name": "Purchase", "type": "customEvent",
         "customEventFilter": [{"type": "equals", "parameter": [{"key": "arg0", "value": "{{_event}}"}, {"key": "arg1", "value": "purchase"}]}]},
        {"triggerId": "11", "name": "Thank you page", "type": "pageview", "filter": [{"type": "contains"}]},
    ],
    "tag": [
        {"name": "Ads - Purchase", "type": "awct", "firingTriggerId": ["10"],
         "parameter": [{"key": "conversionId", "value": "123"}, {"key": "conversionLabel", "value": "abc"}],
         "consentSettings": {"consentStatus": "notNeeded"}},
        {"name": "Google tag", "type": "googtag", "firingTriggerId": ["2147479553"], "parameter": [{"key": "tagId", "value": "G-XYZ"}]},
        {"name": "GA4 - purchase", "type": "gaawe", "firingTriggerId": ["10"], "parameter": [{"key": "eventName", "value": "purchase"}]},
        {"name": "Meta Pixel", "type": "cvt_7_99", "firingTriggerId": ["2147479553"]},
        {"name": "LinkedIn custom", "type": "html", "firingTriggerId": ["11"],
         "parameter": [{"key": "html", "value": "<script>_linkedin_partner_id='1';</script>"}]},
        {"name": "Old UA", "type": "ua", "firingTriggerId": ["2147479553"]},
        {"name": "Orphan", "type": "html", "paused": True, "parameter": [{"key": "html", "value": "<b>hi</b>"}]},
    ],
    "variable": [{"name": "DL - value", "type": "v"}],
}


class GtmTests(unittest.TestCase):
    def test_list_accounts_and_containers(self):
        fake = FakeRoutes({
            "/accounts": {"account": [{"accountId": "1", "name": "Z"}, {"accountId": "2", "name": "a"}]},
            "/accounts/1/containers": {"container": [{"containerId": "5", "publicId": "GTM-X", "name": "Web", "usageContext": ["web"]}]},
        })
        with mock.patch.object(google_rest, "get_json", fake):
            self.assertEqual([a["account_id"] for a in gtm.list_accounts()["accounts"]], ["2", "1"])
            c = gtm.list_containers("1")
        self.assertEqual(c["containers"][0]["public_id"], "GTM-X")
        self.assertEqual(fake.calls[0][2], ("https://www.googleapis.com/auth/tagmanager.readonly",))

    def test_public_id_lookup(self):
        fake = FakeRoutes({
            "containers:lookup": {"accountId": "1", "containerId": "5"},
            "/accounts/1/containers/5/versions:live": LIVE_VERSION,
        })
        with mock.patch.object(google_rest, "get_json", fake):
            out = gtm.live_container_summary(container_id="gtm-abc123")
        self.assertEqual(fake.calls[0][1], {"tagId": "GTM-ABC123"})
        self.assertEqual((out["account_id"], out["container_id"]), ("1", "5"))

    def test_numeric_ids_required_without_public_id(self):
        with self.assertRaises(gtm.GtmInputError):
            gtm.resolve_container("", "5")

    def test_live_summary(self):
        fake = FakeRoutes({"/accounts/1/containers/5/versions:live": LIVE_VERSION})
        with mock.patch.object(google_rest, "get_json", fake):
            out = gtm.live_container_summary("1", "5")
        conv = out["conversion_overview"]
        self.assertEqual(set(conv), {"google_ads", "ga4", "meta", "linkedin", "universal_analytics"})
        self.assertEqual(conv["google_ads"][0]["settings"], {"conversionId": "123", "conversionLabel": "abc"})
        self.assertEqual(conv["google_ads"][0]["fires_on"], ["Purchase"])
        self.assertEqual(conv["meta"][0]["type"], "Facebook Pixel")
        tags = {t["name"]: t for t in out["tags"]}
        self.assertEqual(tags["Google tag"]["firing_triggers"], ["All Pages"])
        self.assertEqual(tags["Google tag"]["platform"], "ga4")
        self.assertEqual(tags["Ads - Purchase"]["consent"], "notNeeded")
        self.assertIn("warning", tags["Orphan"])
        self.assertNotIn("html", json.dumps(tags["LinkedIn custom"]).lower().replace("custom html", ""))
        pts = " ".join(out["attention_points"])
        self.assertIn("Universal Analytics", pts)
        self.assertIn("no firing trigger", pts)
        self.assertIn("Conversion Linker", pts)
        self.assertIn("paused", pts)
        trig = {t["name"]: t for t in out["triggers"]}
        self.assertEqual(trig["Purchase"]["event"], "purchase")
        self.assertEqual(out["counts"], {"tags": 7, "triggers": 2, "variables": 1})
        self.assertEqual(out["public_id"], "GTM-ABC123")


class RegistrationTests(unittest.TestCase):
    def test_tools_registered_read_only(self):
        import asyncio
        from ads_mcp import server

        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in ("merchant_list_accounts", "merchant_account_issues", "merchant_product_status_summary",
                     "merchant_list_products", "gtm_list_accounts", "gtm_list_containers",
                     "gtm_live_container_summary"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name].annotations.readOnlyHint)


if __name__ == "__main__":
    unittest.main()
