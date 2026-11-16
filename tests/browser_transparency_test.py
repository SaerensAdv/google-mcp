"""Pure tests for the public Google Ads Transparency search."""

import asyncio
import unittest

from browser_mcp.capture import browser_slot
from browser_mcp.transparency import (
    build_transparency_report,
    build_transparency_url,
    normalize_region,
    normalize_ad_format,
    parse_ad_count,
    parse_detail_labels,
)


class TransparencyPureTests(unittest.TestCase):
    def test_domain_url(self):
        self.assertEqual(
            build_transparency_url("https://www.example.be/landing", "be"),
            "https://adstransparency.google.com/?domain=www.example.be&region=BE",
        )
        self.assertEqual(
            build_transparency_url("example.be", "anywhere"),
            "https://adstransparency.google.com/?domain=example.be&region=anywhere",
        )

    def test_advertiser_search_url(self):
        self.assertEqual(
            build_transparency_url("Example BV", "NL"),
            "https://adstransparency.google.com/?region=NL",
        )

    def test_region_validation(self):
        self.assertEqual(normalize_region("be"), "BE")
        self.assertEqual(normalize_region("ANYWHERE"), "anywhere")
        with self.assertRaises(Exception):
            normalize_region("Belgium")

    def test_counts(self):
        self.assertEqual(parse_ad_count("74 advertenties"), 74)
        self.assertEqual(parse_ad_count("2 ads"), 2)
        self.assertEqual(parse_ad_count("geen resultaten"), None)

    def test_ad_format_mapping(self):
        self.assertEqual(normalize_ad_format("Tekst"), "text")
        self.assertEqual(normalize_ad_format("Afbeelding"), "image")
        self.assertEqual(normalize_ad_format("Video"), "video")
        self.assertEqual(normalize_ad_format("Onbekend formaat"), "unknown")

    def test_detail_labels(self):
        text = """Advertentiedetails
        Getoond in: België
        Voor het laatst weergegeven
        16 nov 2026
        Indeling: Tekst"""
        self.assertEqual(
            parse_detail_labels(text),
            {"last_shown": "16 nov 2026", "format": "Tekst", "shown_in": "België"},
        )

    def test_report_is_public_and_compact(self):
        report = build_transparency_report(
            "example.be",
            "BE",
            "domain",
            {
                "advertiser": "Example BV",
                "ad_count_visible": 2,
                "cards": [
                    {
                        "creative_url": "https://adstransparency.google.com/advertiser/a/creative/c",
                        "preview_image_url": "https://tpc.googlesyndication.com/archive/simgad/1",
                    }
                ],
                "final_url": "https://adstransparency.google.com/?domain=example.be&region=BE",
            },
            1.2,
        )
        self.assertEqual(report["advertiser"], "Example BV")
        self.assertEqual(report["ads_returned"], 1)
        self.assertTrue(report["ads"][0]["creative_url"].startswith("https://"))
        self.assertTrue(report["notes"])
        self.assertNotIn("spend", report)


class BrowserQueueTests(unittest.TestCase):
    def test_second_browser_job_waits_for_first(self):
        async def scenario():
            entered = []

            async def job(name, delay):
                async with browser_slot():
                    entered.append(name)
                    await asyncio.sleep(delay)

            first = asyncio.create_task(job("first", 0.05))
            await asyncio.sleep(0.005)
            second = asyncio.create_task(job("second", 0))
            await asyncio.sleep(0.01)
            self.assertEqual(entered, ["first"])
            await asyncio.gather(first, second)
            self.assertEqual(entered, ["first", "second"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
