"""Unit tests for the tracking/consent check and page audit (pure parts, no browser)."""

import unittest

from browser_mcp import consent as cmp
from browser_mcp import sitecheck as sc
from browser_mcp import tracking as tr


def _hit(platform, phase="before", kind="hit", id_=None, event=None, gcs=None, gcd=None):
    h = {"platform": platform, "kind": kind, "id": id_, "event": event, "beacon": kind == "hit", "phase": phase}
    consent = {k: v for k, v in (("gcs", gcs), ("gcd", gcd)) if v}
    if consent:
        h["consent"] = consent
    return h


class ClassifyTests(unittest.TestCase):
    def test_ga4_batched_post(self):
        url = "https://region1.google-analytics.com/g/collect?v=2&tid=G-ABC123&gcs=G100&gcd=13p3p3p2p5l1"
        hits = tr.classify_request(url, "POST", "en=page_view&_et=1\nen=scroll&epn.percent_scrolled=90")
        self.assertEqual([(h["platform"], h["id"], h["event"]) for h in hits],
                         [("GA4", "G-ABC123", "page_view"), ("GA4", "G-ABC123", "scroll")])
        self.assertEqual(hits[0]["consent"], {"gcs": "G100", "gcd": "13p3p3p2p5l1"})
        self.assertTrue(tr.is_beacon(url, "POST", "en=page_view"))

    def test_server_side_ga4_on_custom_domain(self):
        hits = tr.classify_request("https://sst.example.be/g/collect?v=2&tid=G-XYZ&en=generate_lead")
        self.assertEqual(hits[0]["platform"], "GA4")
        self.assertEqual(hits[0]["event"], "generate_lead")

    def test_google_ads(self):
        conv = tr.classify_request("https://www.googleadservices.com/pagead/conversion/123456789/?label=AbC&gcs=G111")
        self.assertEqual((conv[0]["platform"], conv[0]["id"], conv[0]["event"]), ("Google Ads", "AW-123456789", "conversion (AbC)"))
        rm = tr.classify_request("https://googleads.g.doubleclick.net/pagead/viewthroughconversion/123456789/?x=1")
        self.assertEqual(rm[0]["event"], "remarketing")
        self.assertEqual(tr.classify_request("https://www.google.com/rmkt/collect/123/")[0]["id"], "AW-123")
        self.assertEqual(tr.classify_request("https://www.google.com/pagead/1p-conversion/42/?label=L")[0]["id"], "AW-42")

    def test_loaders_are_not_beacons(self):
        gtm = tr.classify_request("https://www.googletagmanager.com/gtm.js?id=GTM-ABCD")
        self.assertEqual((gtm[0]["platform"], gtm[0]["id"], gtm[0]["kind"]), ("Google Tag Manager", "GTM-ABCD", "library"))
        self.assertFalse(tr.is_beacon("https://www.googletagmanager.com/gtag/js?id=G-1"))
        self.assertFalse(tr.is_beacon("https://connect.facebook.net/en_US/fbevents.js"))

    def test_other_pixels(self):
        meta = tr.classify_request("https://www.facebook.com/tr/?id=999&ev=Lead&dl=x")
        self.assertEqual((meta[0]["platform"], meta[0]["id"], meta[0]["event"]), ("Meta Pixel", "999", "Lead"))
        self.assertEqual(tr.classify_request("https://px.ads.linkedin.com/collect?pid=55&fmt=js")[0]["id"], "55")
        self.assertEqual(tr.classify_request("https://bat.bing.com/action/0?ti=777&evt=pageLoad")[0]["event"], "pageLoad")
        tt = tr.classify_request("https://analytics.tiktok.com/api/v2/pixel", "POST",
                                 '{"event":"ViewContent","context":{"pixel":{"code":"C123"}}}')
        self.assertEqual((tt[0]["id"], tt[0]["event"]), ("C123", "ViewContent"))
        self.assertEqual(tr.classify_request("https://www.example.com/app.js"), [])
        self.assertEqual(tr.classify_request("not a url"), [])


class ConsentModeTests(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(tr.decode_gcs("G101"), {"ad_storage": "denied", "analytics_storage": "granted"})
        self.assertIsNone(tr.decode_gcs("X"))
        d = tr.decode_gcd("13r3r3r2r5l1")
        self.assertEqual((d["ad_storage"], d["ad_user_data"], d["default_set"]), ("granted", "granted", True))
        self.assertEqual(tr.decode_gcd("13l3l3l3l1")["default_set"], False)
        self.assertIsNone(tr.decode_gcd("1"))
        merged = tr.consent_state({"gcs": "G100", "gcd": "13p3p3l2l5l1"})
        self.assertEqual(merged["ad_user_data"], "not set")

    def test_cookies(self):
        cookies = [{"name": "_ga", "domain": ".x.be", "value": "SECRET"}, {"name": "_fbp", "domain": ".x.be"},
                   {"name": "CookieConsent", "domain": "x.be"}, {"name": "_ga", "domain": ".x.be"}]
        out = tr.tracking_cookies(cookies)
        self.assertEqual([c["name"] for c in out], ["_ga", "_fbp"])
        self.assertNotIn("value", out[0])


class FindingsTests(unittest.TestCase):
    def test_before_consent(self):
        hits = [_hit("Meta Pixel", event="PageView"), _hit("GA4", gcs="G100"), _hit("Google Ads"),
                _hit("Google Tag Manager", kind="library", id_="GTM-1")]
        f = tr.phase_findings("before", hits, [{"name": "_fbp", "vendor": "Meta", "category": "marketing"}])
        self.assertTrue(any("Meta Pixel verstuurt data vóór" in x for x in f))
        self.assertTrue(any("Google Ads-hits" in x and "zonder Consent Mode" in x for x in f))
        self.assertFalse(any(x.startswith("GA4") for x in f))  # denied cookieless ping is fine
        self.assertTrue(any("_fbp" in x for x in f))

    def test_accept(self):
        f = tr.accept_findings([_hit("GA4", "after_accept", gcd="13p3p3l2l5l1")], {"GA4"})
        self.assertTrue(any("blijft consent 'denied'" in x for x in f))
        self.assertTrue(any("Consent Mode v2-signalen ontbreken" in x for x in f))
        self.assertEqual(tr.accept_findings([], {"GA4"}), ["GA4-tag aanwezig maar na ACCEPTEREN geen GA4-hit gezien."])

    def test_report(self):
        reject = {"final_url": "https://x.be/", "http_status": 200, "consent_click": {"clicked": True},
                  "cmps_loaded": ["Cookiebot"], "beacons_answered_locally": 2, "writes_blocked": ["POST a"],
                  "cmp_geo": {"Cookiebot": {"country": "be", "gdpr_applies": True}},
                  "phases": {"before_consent": {"platforms": [], "tracking_cookies": []},
                             "after_reject": {"platforms": [], "new_tracking_cookies": []}},
                  "_hits": [_hit("GA4", "before", id_="G-1", gcs="G100", gcd="13l3l3l3l1"),
                            _hit("Meta Pixel", "after_reject", id_="9", event="PageView")],
                  "datalayer": {"containers": ["GTM-AAA"], "items": []}}
        accept = {"consent_click": {"clicked": True}, "beacons_answered_locally": 3, "writes_blocked": ["POST a"],
                  "phases": {"before_consent": {}, "after_accept": {}, "after_click": {}},
                  "click": {"clicked": False, "error": "Element niet gevonden."},
                  "_hits": [_hit("GA4", "after_accept", id_="G-1", gcs="G111", gcd="13v3v3v2v5l1")],
                  "datalayer": {"containers": [], "items": [{"command": "consent", "arg": "default"}]}}
        r = sc.build_tracking_report("https://x.be/", "desktop", "compare", {"reject": reject, "accept": accept},
                                     "Bel ons", "US")
        self.assertIsNone(r["geo_warning"])  # Cookiebot applies GDPR, so the US server is not a problem
        self.assertEqual(r["tags_found"]["Google Tag Manager"], ["GTM-AAA"])
        self.assertIn("Consent Mode: geen 'default'-consent ingesteld vóór de Google-tags vuren.", r["findings"])
        self.assertTrue(any("Meta Pixel verstuurt data na WEIGEREN" in x for x in r["findings"]))
        self.assertTrue(any("Klik-stap (accept)" in x for x in r["findings"]))
        self.assertEqual(r["safety"]["tracking_hits_answered_locally"], 5)
        self.assertEqual(r["safety"]["write_requests_blocked"], ["POST a"])
        self.assertIn("after_click (accept)", r["phases"])
        self.assertEqual(r["datalayer"]["consent_commands"], [{"command": "consent", "arg": "default"}])
        self.assertNotIn("_hits", str(r.keys()))

    def test_geo_warning(self):
        s = {"phases": {}, "_hits": [], "cmp_geo": {"Cookiebot": {"country": "us-45", "gdpr_applies": False}}}
        r = sc.build_tracking_report("https://x.be/", "desktop", "none", {"none": s}, None, "US")
        self.assertIn("NIET toe", r["geo_warning"])
        r2 = sc.build_tracking_report("https://x.be/", "desktop", "none", {"none": {"phases": {}, "_hits": []}}, None, "US")
        self.assertIn("server in US", r2["geo_warning"])
        r3 = sc.build_tracking_report("https://x.be/", "desktop", "none", {"none": {"phases": {}, "_hits": []}}, None, "BE")
        self.assertIsNone(r3["geo_warning"])


class ConsentButtonTests(unittest.TestCase):
    def test_text_patterns(self):
        for t in ("Alles accepteren", "Alle cookies accepteren", "Akkoord", "Accept all", "Tout accepter", "OK"):
            self.assertRegex(t, cmp.ACCEPT_TEXT)
        for t in ("Alles weigeren", "Weigeren", "Alleen noodzakelijke cookies", "Reject all", "Tout refuser"):
            self.assertRegex(t, cmp.REJECT_TEXT)
        for t in ("Meer informatie", "Instellingen", "Lees onze privacyverklaring over cookies accepteren"):
            self.assertNotRegex(t, cmp.ACCEPT_TEXT)
            self.assertNotRegex(t, cmp.REJECT_TEXT)

    def test_cmp_from_url_and_banner_findings(self):
        self.assertEqual(cmp.cmp_from_url("https://consent.cookiebot.com/uc.js"), "Cookiebot")
        self.assertEqual(cmp.cmp_from_url("https://x.be/wp-content/plugins/complianz-gdpr/x.js"), "Complianz")
        self.assertIsNone(cmp.cmp_from_url("https://x.be/app.js"))
        f = cmp.banner_findings({"clicked": True}, {"clicked": False}, [])
        self.assertTrue(any("weigeren" in x for x in f))
        self.assertTrue(any("accepteren" in x for x in cmp.banner_findings({"clicked": False}, None, ["OneTrust"])))


class AuditReportTests(unittest.TestCase):
    def test_build(self):
        col = sc.AuditCollector("example.be")
        col.on_will_send({"requestId": "1", "request": {"url": "https://www.example.be/"}, "type": "Document"})
        col.on_response({"requestId": "1", "response": {"url": "https://www.example.be/", "status": 200}, "type": "Document"})
        col.on_finished({"requestId": "1", "encodedDataLength": 50_000})
        col.on_response({"requestId": "2", "response": {"url": "https://cdn.vendor.com/big.js", "status": 200}, "type": "Script"})
        col.on_finished({"requestId": "2", "encodedDataLength": 3_300_000})
        col.on_response({"requestId": "3", "response": {"url": "https://www.example.be/missing.png", "status": 404}, "type": "Image"})
        col.on_failed({"requestId": "4", "errorText": "net::ERR_FAILED", "type": "Fetch"})
        col.requests["4"]["url"] = "https://www.google-analytics.com/g/collect?x"
        col.on_issue({"issue": {"code": "MixedContentIssue",
                                "details": {"mixedContentIssueDetails": {"insecureURL": "http://www.example.be/a.jpg"}}}})
        col.on_page_error("TypeError: x is undefined\n at y")
        perf = {"ttfb": 100, "fcp": 900, "lcp": {"t": 5000, "tag": "IMG", "url": "https://www.example.be/hero.jpg"},
                "cls": 0.3, "tbt": 100, "blocking": [], "html": {"title": "", "meta_description": False, "h1": 2,
                "viewport": True, "robots": "noindex", "img_no_alt": 1, "img_broken": []}}
        r = sc.build_audit_report("https://www.example.be/", "desktop", {"http_status": 200, "final_url": "https://www.example.be/"},
                                  perf, col, {"https://www.google-analytics.com/g/collect?x"})
        self.assertEqual(r["page_weight"]["third_parties"][0]["domain"], "vendor.com")
        self.assertEqual(r["performance_lab"]["ratings"]["lcp"], "slecht")
        self.assertEqual(len(r["errors"]["failed_requests"]), 1)  # locally answered beacon ignored
        self.assertEqual(r["errors"]["failed_requests"][0]["status"], 404)
        joined = " ".join(r["findings"])
        for needle in ("JavaScript-fout", "LCP slecht", "CLS slecht", "Zware pagina", "noindex", "Geen <title>",
                       "2 H1", "Mixed content", "zonder alt"):
            self.assertIn(needle, joined)
        self.assertEqual(r["devtools_issues"][0]["examples"], ["http://www.example.be/a.jpg"])

    def test_site_key(self):
        self.assertEqual(sc._site_key("www.bbc.co.uk"), "bbc.co.uk")
        self.assertEqual(sc._site_key("a.b.example.be"), "example.be")


class RegistrationTests(unittest.TestCase):
    def test_tools_registered_read_only(self):
        import asyncio
        from ads_mcp import server

        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in ("browser_tracking_check", "browser_page_audit"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name].annotations.readOnlyHint)


if __name__ == "__main__":
    unittest.main()
