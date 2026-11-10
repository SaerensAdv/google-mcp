"""Unit tests for the Keyword Planner tools (mocked Google Ads client)."""

import json
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ads_mcp import keyword_planner as kp


class _Enum:
    def __init__(self, name):
        self.name = name


class _Repeated(list):
    def extend(self, items):  # mimic proto repeated field
        super().extend(items)


class _Req:
    """Loose stand-in for a proto request: any attribute auto-creates."""

    def __init__(self):
        self.__dict__["_d"] = {}

    def __getattr__(self, name):
        d = self.__dict__["_d"]
        if name not in d:
            d[name] = _Repeated() if name in ("geo_target_constants", "keywords", "names") else _Req()
        return d[name]

    def __setattr__(self, name, value):
        self.__dict__["_d"][name] = value

    def has(self, name):
        return name in self.__dict__["_d"]


def _metrics(avg=0, comp="LOW", idx=0, low=0, high=0, months=()):
    return NS(
        avg_monthly_searches=avg,
        competition=_Enum(comp),
        competition_index=idx,
        low_top_of_page_bid_micros=low,
        high_top_of_page_bid_micros=high,
        monthly_search_volumes=[
            NS(year=y, month=_Enum(m), monthly_searches=s) for y, m, s in months
        ],
    )


class FakeClient:
    def __init__(self, ideas=(), historical=(), currency="EUR", languages=(), geo=()):
        self.requests = {}
        self.ideas = list(ideas)
        self.historical = list(historical)
        self.currency = currency
        self.languages = list(languages)
        self.geo = list(geo)
        self.enums = NS(KeywordPlanNetworkEnum=NS(GOOGLE_SEARCH="GOOGLE_SEARCH"))
        self.search_queries = []

    def get_type(self, name):
        req = _Req()
        self.requests[name] = req
        return req

    def get_service(self, name):
        client = self

        if name == "GoogleAdsService":
            def search(customer_id, query):
                client.search_queries.append(query)
                if "FROM customer" in query:
                    return [NS(customer=NS(currency_code=client.currency))]
                if "FROM language_constant" in query:
                    return [NS(language_constant=NS(id=i, name=n, code=c)) for i, n, c in client.languages]
                return []
            return NS(search=search)

        if name == "GeoTargetConstantService":
            def suggest(request):
                return NS(geo_target_constant_suggestions=client.geo)
            return NS(suggest_geo_target_constants=suggest)

        if name == "KeywordPlanIdeaService":
            return NS(
                generate_keyword_ideas=lambda request: iter(client.ideas),
                generate_keyword_historical_metrics=lambda request: NS(results=client.historical),
            )
        raise AssertionError(f"unexpected service {name}")


def _geo(term, rid, name, canonical, reach, typ="City"):
    return NS(
        search_term=term,
        reach=reach,
        geo_target_constant=NS(
            resource_name=f"geoTargetConstants/{rid}", name=name,
            canonical_name=canonical, target_type=typ,
        ),
    )


class InputTests(unittest.TestCase):
    def test_customer_id_hyphens_removed(self):
        self.assertEqual(kp.clean_customer_id("847-524-5227"), "8475245227")

    def test_customer_id_invalid(self):
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.clean_customer_id("abc")

    def test_keywords_deduplicated_and_capped(self):
        self.assertEqual(kp.clean_keywords([" a  b ", "A B", "", "c"], 5), ["a b", "c"])
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.clean_keywords([str(i) for i in range(6)], 5)

    def test_language_by_id_needs_no_lookup(self):
        c = FakeClient()
        self.assertEqual(kp.resolve_language(c, "1", "1010")["name"], "Dutch")
        self.assertEqual(kp.resolve_language(c, "1", "languageConstants/1002")["id"], "1002")
        self.assertEqual(c.search_queries, [])

    def test_language_by_dutch_name_and_code(self):
        c = FakeClient(languages=[(1010, "Dutch", "nl"), (1002, "French", "fr")])
        self.assertEqual(kp.resolve_language(c, "1", "Nederlands")["id"], "1010")
        self.assertEqual(kp.resolve_language(c, "1", "fr")["id"], "1002")
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.resolve_language(c, "1", "Klingon")

    def test_location_prefers_exact_name_over_first_suggestion(self):
        c = FakeClient(geo=[
            _geo("Gent", 9058426, "Gentry County", "Gentry County,Missouri,United States", 24000, "County"),
            _geo("Gent", 1001208, "Gent", "Ghent,Flanders,Belgium", 2550000),
        ])
        locs = kp.resolve_locations(c, ["Gent", "2056"])
        self.assertEqual([l["id"] for l in locs], ["2056", "1001208"])
        self.assertEqual(locs[0]["name"], "Belgium")

    def test_unknown_location(self):
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.resolve_locations(FakeClient(geo=[]), ["Nergenshuizen"])

    def test_too_many_locations(self):
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.resolve_locations(FakeClient(), [str(i) for i in range(11)])


class KeywordIdeasTests(unittest.TestCase):
    def _client(self):
        return FakeClient(ideas=[
            NS(text="b small", keyword_idea_metrics=_metrics(10, "LOW", 5, 0, 0)),
            NS(text="a big", keyword_idea_metrics=_metrics(1600, "HIGH", 90, 300000, 1470000)),
            NS(text="c mid", keyword_idea_metrics=_metrics(390, "UNSPECIFIED", 0, 1110000, 2940000)),
        ])

    def test_sorted_capped_and_money_converted(self):
        c = self._client()
        out = kp.generate_keyword_ideas(c, "123-456-7890", keywords=["x"], limit=2)
        self.assertEqual(out["currency"], "EUR")
        self.assertEqual(out["total_ideas_found"], 3)
        self.assertEqual([i["keyword"] for i in out["ideas"]], ["a big", "c mid"])
        top = out["ideas"][0]
        self.assertEqual(top["low_top_of_page_bid"], 0.3)
        self.assertEqual(top["high_top_of_page_bid"], 1.47)
        self.assertEqual(top["competition"], "HIGH")
        self.assertIsNone(out["ideas"][1]["competition"])
        self.assertEqual(out["locations"], [{"id": "2056", "name": "Belgium"}])
        self.assertEqual(out["language"], {"id": "1010", "name": "Dutch"})
        json.dumps(out)  # serialisable

    def test_zero_bid_is_null(self):
        out = kp.generate_keyword_ideas(self._client(), "1", keywords=["x"])
        small = [i for i in out["ideas"] if i["keyword"] == "b small"][0]
        self.assertIsNone(small["low_top_of_page_bid"])

    def test_seed_types(self):
        c = self._client()
        kp.generate_keyword_ideas(c, "1", keywords=["x"])
        req = c.requests["GenerateKeywordIdeasRequest"]
        self.assertEqual(list(req.keyword_seed.keywords), ["x"])
        self.assertEqual(req.keyword_plan_network, "GOOGLE_SEARCH")

        c = self._client()
        kp.generate_keyword_ideas(c, "1", page_url="example.com")
        req = c.requests["GenerateKeywordIdeasRequest"]
        self.assertEqual(req.url_seed.url, "https://example.com")
        self.assertFalse(req.has("keyword_seed"))

        c = self._client()
        kp.generate_keyword_ideas(c, "1", keywords=["x"], page_url="https://example.com/a")
        req = c.requests["GenerateKeywordIdeasRequest"]
        self.assertEqual(req.keyword_and_url_seed.url, "https://example.com/a")
        self.assertEqual(list(req.keyword_and_url_seed.keywords), ["x"])

    def test_requires_seed(self):
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.generate_keyword_ideas(self._client(), "1")

    def test_limit_bounds(self):
        out = kp.generate_keyword_ideas(self._client(), "1", keywords=["x"], limit=0)
        self.assertEqual(out["count"], 1)


class HistoricalMetricsTests(unittest.TestCase):
    def test_trend_last_12_months_sorted_and_no_data(self):
        months = [(2025, "NOVEMBER", 4400), (2025, "OCTOBER", 3600)] + [
            (2026, m, 100) for m in ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY",
                                     "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER"]
        ] + [(2024, "DECEMBER", 1)]
        c = FakeClient(historical=[
            NS(text="small", close_variants=[], keyword_metrics=_metrics(20)),
            NS(text="tandarts gent", close_variants=["gent tandarts", "tandarts gent"],
               keyword_metrics=_metrics(3600, "LOW", 21, 300000, 1840000, months)),
            NS(text="nonsense", close_variants=[], keyword_metrics=_metrics(0)),
        ])
        out = kp.generate_historical_metrics(
            c, "1", ["tandarts gent", "Gent Tandarts", "small", "nonsense", "missing"]
        )
        self.assertEqual([k["keyword"] for k in out["keywords"]], ["tandarts gent", "small"])
        top = out["keywords"][0]
        self.assertEqual(top["close_variants"], ["gent tandarts"])
        self.assertEqual(len(top["monthly_searches"]), 12)
        self.assertEqual(top["monthly_searches"][0], {"month": "2025-10", "searches": 3600})
        self.assertEqual(top["monthly_searches"][-1]["month"], "2026-10")
        self.assertEqual(top["high_top_of_page_bid"], 1.84)
        self.assertEqual(out["no_data"], ["nonsense", "missing"])

    def test_requires_keywords_and_caps(self):
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.generate_historical_metrics(FakeClient(), "1", [])
        with self.assertRaises(kp.KeywordPlannerInputError):
            kp.generate_historical_metrics(FakeClient(), "1", [str(i) for i in range(101)])


class ErrorTranslationTests(unittest.TestCase):
    def _ex(self, message):
        from google.ads.googleads.errors import GoogleAdsException

        ex = GoogleAdsException.__new__(GoogleAdsException)
        ex.request_id = "req-1"
        ex.failure = NS(errors=[NS(message=message)])
        return ex

    def test_input_error_becomes_tool_error(self):
        from fastmcp.exceptions import ToolError

        with mock.patch("ads_mcp.utils.get_googleads_client", return_value=FakeClient()):
            with self.assertRaisesRegex(ToolError, "seed keyword"):
                kp._run(kp.generate_keyword_ideas, "1")

    def test_ads_error_with_rate_limit_hint(self):
        from fastmcp.exceptions import ToolError

        def boom(client, *a, **k):
            raise self._ex("Resource has been exhausted (e.g. check quota). RESOURCE_EXHAUSTED")

        with mock.patch("ads_mcp.utils.get_googleads_client", return_value=FakeClient()):
            with self.assertRaises(ToolError) as cm:
                kp._run(boom)
        msg = str(cm.exception)
        self.assertIn("Request ID: req-1", msg)
        self.assertIn("rate limit", msg)

    def test_ads_error_plain(self):
        from fastmcp.exceptions import ToolError

        def boom(client, *a, **k):
            raise self._ex("The customer is not enabled.")

        with mock.patch("ads_mcp.utils.get_googleads_client", return_value=FakeClient()):
            with self.assertRaises(ToolError) as cm:
                kp._run(boom)
        self.assertNotIn("rate limit", str(cm.exception))


class RegistrationTests(unittest.TestCase):
    def test_tools_registered_read_only(self):
        import asyncio
        from ads_mcp import server

        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in ("keyword_ideas", "keyword_historical_metrics"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name].annotations.readOnlyHint)


if __name__ == "__main__":
    unittest.main()
