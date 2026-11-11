"""Site checks in the hardened headless browser (read-only towards the site):

  * run_tracking_check  - which tags/pixels fire before consent, after reject,
                          after accept and after an optional click; Consent Mode.
  * run_page_audit      - DevTools-style issues, console/JS errors, failed
                          requests, lab performance and page weight/third parties.

Safety: every visit is a fresh isolated session behind the GuardProxy. Tracking
beacons (GA4/Ads/Meta/... data hits) are recorded and answered locally with 204,
so no test traffic reaches the client's analytics/ads accounts. All non-GET
requests (form posts, AJAX writes) are blocked, so the site never receives data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from browser_mcp import consent as cmp
from browser_mcp import tracking as tr
from browser_mcp.capture import (
    NAV_TIMEOUT_MS, VIEWPORTS, CaptureError, _is_browser_internal, _safe_url_for_log,
    browser_slot, check_public_url, hardened_browser, new_hardened_context,
)

logger = logging.getLogger(__name__)

TRACKING_TIMEOUT_S = 110
AUDIT_TIMEOUT_S = 75
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

_DATALAYER_JS = """
() => {
  const out = {dl: null, containers: []};
  try {
    out.containers = Object.keys(window.google_tag_manager || {}).filter(k => /^(GTM|G|AW|DC)-/.test(k)).slice(0, 20);
  } catch (e) {}
  const dl = window.dataLayer;
  if (!Array.isArray(dl)) return out;
  out.dl = dl.slice(0, 80).map(it => {
    try {
      if (it && typeof it === 'object' && typeof it.length === 'number' && typeof it[0] === 'string') {
        const params = (it[0] === 'consent' && it[2] && typeof it[2] === 'object')
          ? Object.fromEntries(Object.entries(it[2]).slice(0, 12).map(([k, v]) => [k, String(v)])) : null;
        return {_cmd: it[0], arg: typeof it[1] === 'string' ? it[1] : '', params};
      }
      if (it && typeof it === 'object' && it.event) {
        return {event: String(it.event), keys: Object.keys(it).slice(0, 20)};
      }
    } catch (e) {}
    return null;
  }).filter(Boolean);
  return out;
}
"""

_CMP_GEO_JS = """
() => {
  const o = {};
  try { const c = window.Cookiebot; if (c && c.regulations) o.Cookiebot = {country: String(c.userCountry || ''), gdpr_applies: !!c.regulations.gdprApplies}; } catch (e) {}
  try { if (window.OneTrust && OneTrust.getGeolocationData) { const g = OneTrust.getGeolocationData() || {}; o.OneTrust = {country: String(g.country || '')}; } } catch (e) {}
  try { if (window.Didomi && Didomi.notice && Didomi.notice.isVisible) o.Didomi = {notice_visible: !!Didomi.notice.isVisible()}; } catch (e) {}
  return o;
}
"""

EEA = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
       "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH", "GB"}
_egress_cache: Dict[str, Any] = {}


async def egress_country() -> Optional[str]:
    """Country of this server's outgoing IP (what CMPs geolocate). Cached 6h; None if unknown."""
    import httpx

    now = time.monotonic()
    if _egress_cache.get("at") and now - _egress_cache["at"] < 6 * 3600:
        return _egress_cache.get("country")
    country = None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
        m = re.search(r"^loc=([A-Z]{2})$", resp.text, re.M)
        country = m.group(1) if m else None
    except Exception as e:  # noqa: BLE001
        logger.info("egress country lookup failed: %s", type(e).__name__)
    _egress_cache.update(at=now, country=country)
    return country


_IS_SUBMIT_JS = """
el => {
  const t = (el.getAttribute('type') || '').toLowerCase();
  if (el.tagName === 'INPUT' && (t === 'submit' || t === 'image')) return true;
  if (el.tagName === 'BUTTON' && el.form && (t === '' || t === 'submit')) return true;
  return false;
}
"""


def _short(url: str, n: int = 160) -> str:
    p = urlsplit(url)
    s = f"{p.scheme}://{p.hostname}{p.path}"
    return s if len(s) <= n else s[: n - 1] + "…"


def _cors_headers(req) -> Dict[str, str]:
    try:
        origin = req.headers.get("origin")
    except Exception:  # noqa: BLE001
        origin = None
    if not origin:
        return {}
    return {"access-control-allow-origin": origin, "access-control-allow-credentials": "true"}


class Recorder:
    """Records tracking requests per phase; answers beacons locally; blocks writes."""

    def __init__(self) -> None:
        self.phase = "before"
        self.hits: List[Dict[str, Any]] = []
        self.cmps: List[str] = []
        self.beacons_answered = 0
        self.writes_blocked: List[str] = []
        self.answered_urls: set = set()

    def on_request(self, request) -> None:
        try:
            post = request.post_data if request.method != "GET" else None
        except Exception:  # noqa: BLE001 — binary bodies
            post = None
        url = request.url
        for h in tr.classify_request(url, request.method, post):
            h["phase"] = self.phase
            self.hits.append(h)
        name = cmp.cmp_from_url(url)
        if name and name not in self.cmps:
            self.cmps.append(name)

    async def route(self, route) -> None:
        req = route.request
        try:
            post = req.post_data if req.method != "GET" else None
        except Exception:  # noqa: BLE001
            post = None
        try:
            if tr.is_beacon(req.url, req.method, post):
                self.beacons_answered += 1
                self.answered_urls.add(req.url)
                await route.fulfill(status=204, body="", headers=_cors_headers(req))
            elif req.method.upper() not in SAFE_METHODS and "/cdn-cgi/challenge-platform/" not in req.url:
                entry = f"{req.method} {_short(req.url, 120)}"
                if len(self.writes_blocked) < 30 and entry not in self.writes_blocked:
                    self.writes_blocked.append(entry)
                self.answered_urls.add(req.url)
                # Answered locally (never sent): the page sees a normal empty success.
                await route.fulfill(status=204, body="", headers=_cors_headers(req))
            else:
                await route.continue_()
        except Exception as e:  # noqa: BLE001 — page/context closing
            logger.debug("route handling failed: %s", type(e).__name__)

    def phase_hits(self, phase: str) -> List[Dict[str, Any]]:
        return [h for h in self.hits if h["phase"] == phase]


async def _settle(page, idle_ms: int = 4000, extra_ms: int = 2500) -> None:
    from playwright.async_api import Error as PWError, TimeoutError as PWTimeout

    with contextlib.suppress(PWTimeout, PWError):
        await page.wait_for_load_state("networkidle", timeout=idle_ms)
    with contextlib.suppress(PWError):
        await page.wait_for_timeout(extra_ms)


async def _goto(page, url: str, proxy) -> Dict[str, Any]:
    from playwright.async_api import Error as PWError, TimeoutError as PWTimeout

    info: Dict[str, Any] = {"http_status": None, "warnings": []}
    try:
        resp = await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        if resp:
            info["http_status"] = resp.status
            chain = []
            req = resp.request.redirected_from
            while req is not None and len(chain) < 10:
                r = await req.response()
                chain.append({"url": _short(req.url), "status": r.status if r else None})
                req = req.redirected_from
            if chain:
                info["redirects"] = list(reversed(chain))
    except PWTimeout:
        info["warnings"].append(f"Load-event niet binnen {NAV_TIMEOUT_MS // 1000}s; resultaat mogelijk onvolledig.")
    except PWError as e:
        msg = str(e).split("\n")[0]
        raise CaptureError(
            f"Navigatie mislukt: {msg}"
            + (f" | Geblokkeerd door netwerkbeveiliging: {proxy.blocked}" if proxy.blocked else "")
        ) from None
    if page.url in ("", "about:blank"):
        raise CaptureError("Pagina kon niet geladen worden (timeout vóór eerste inhoud).")
    return info


async def _is_bot_challenge(page, status: Optional[int]) -> bool:
    if status not in (403, 429, 503):
        return False
    with contextlib.suppress(Exception):
        title = (await page.title()).lower()
        if any(t in title for t in ("just a moment", "even geduld", "attention required", "access denied", "captcha")):
            return True
        return bool(await page.locator("script[src*='/cdn-cgi/challenge-platform/'], #challenge-form, iframe[src*='captcha']").count())
    return False


async def _snapshot(page, context, rec: Recorder, phase: str, known_cookies: set) -> Dict[str, Any]:
    cookies = []
    with contextlib.suppress(Exception):
        cookies = tr.tracking_cookies(await context.cookies())
    new = [c for c in cookies if (c["name"], c["domain"]) not in known_cookies]
    for c in cookies:
        known_cookies.add((c["name"], c["domain"]))
    return {
        "platforms": tr.summarize_hits(rec.phase_hits(phase)),
        "tracking_cookies": cookies,
        "new_tracking_cookies": new,
    }


async def _read_datalayer(page) -> Dict[str, Any]:
    try:
        raw = await asyncio.wait_for(page.evaluate(_DATALAYER_JS), 5)
    except Exception:  # noqa: BLE001
        return {"containers": [], "items": []}
    return {
        "containers": [str(c)[:30] for c in (raw or {}).get("containers") or []][:20],
        "items": tr.compact_datalayer((raw or {}).get("dl")),
    }


async def _do_click(page, target: str) -> Dict[str, Any]:
    target = (target or "").strip()[:200]
    if target.lower().startswith("css="):
        locator = page.locator(target[4:].strip())
        how = f"CSS '{target[4:].strip()[:80]}'"
    else:
        pattern = re.compile(re.escape(target), re.I)
        locator = None
        for role in ("button", "link"):
            cand = page.get_by_role(role, name=pattern)
            with contextlib.suppress(Exception):
                if await cand.count():
                    locator = cand
                    break
        if locator is None:
            locator = page.get_by_text(pattern)
        how = f"tekst '{target[:80]}'"
    el = await cmp._first_visible(locator)
    if el is None:
        return {"clicked": False, "target": how, "error": "Element niet gevonden of niet zichtbaar."}
    try:
        if await el.evaluate(_IS_SUBMIT_JS):
            return {"clicked": False, "target": how,
                    "error": "Geweigerd: dit is een verzendknop van een formulier (geen formulieren versturen)."}
        href = await el.get_attribute("href")
        await el.click(timeout=5000)
    except Exception as e:  # noqa: BLE001
        hint = " Mogelijk bedekt door de cookiebanner: gebruik consent 'accept'." if "Timeout" in type(e).__name__ else ""
        return {"clicked": False, "target": how, "error": f"Klik mislukt ({type(e).__name__}).{hint}"}
    out = {"clicked": True, "target": how}
    if href:
        out["href"] = href[:200]
    return out


async def _tracking_session(browser, proxy, url: str, device: str, action: Optional[str],
                            click: Optional[str]) -> Dict[str, Any]:
    context, page = await new_hardened_context(browser, device)
    rec = Recorder()
    page.on("request", rec.on_request)
    known: set = set()
    out: Dict[str, Any] = {"phases": {}}
    try:
        await context.route("**/*", rec.route)
        nav = await _goto(page, url, proxy)
        out.update(nav)
        out["final_url"] = page.url
        await _settle(page)
        out["bot_challenge"] = await _is_bot_challenge(page, nav.get("http_status"))
        out["phases"]["before_consent"] = await _snapshot(page, context, rec, "before", known)
        dl_before = await _read_datalayer(page)
        with contextlib.suppress(Exception):
            out["cmp_geo"] = await asyncio.wait_for(page.evaluate(_CMP_GEO_JS), 5)
        if action:
            out["consent_click"] = await cmp.click_consent(page, action)
            rec.phase = f"after_{action}"
            await _settle(page)
            out["phases"][rec.phase] = await _snapshot(page, context, rec, rec.phase, known)
        if click:
            rec.phase = "after_click"
            out["click"] = await _do_click(page, click)
            await _settle(page)
            out["phases"]["after_click"] = await _snapshot(page, context, rec, "after_click", known)
        dl_after = await _read_datalayer(page)
        out["datalayer"] = dl_after if dl_after["items"] or dl_after["containers"] else dl_before
        out["cmps_loaded"] = rec.cmps
        out["beacons_answered_locally"] = rec.beacons_answered
        out["writes_blocked"] = rec.writes_blocked
        out["_hits"] = rec.hits
        return out
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(context.close(), 10)


def _tags_found(hits: List[Dict[str, Any]], containers: List[str]) -> Dict[str, List[str]]:
    tags: Dict[str, List[str]] = {}
    for h in hits:
        if h.get("id"):
            lst = tags.setdefault(h["platform"], [])
            if h["id"] not in lst:
                lst.append(h["id"])
    for c in containers:
        key = "Google Tag Manager" if c.startswith("GTM-") else "Google tag (gtag.js)"
        lst = tags.setdefault(key, [])
        if c not in lst:
            lst.append(c)
    return tags


def build_tracking_report(url: str, device: str, consent: str, sessions: Dict[str, Dict[str, Any]],
                          click: Optional[str], visitor_country: Optional[str] = None) -> Dict[str, Any]:
    """Merge 1-2 sessions into one report with findings (pure; unit-tested)."""
    first = sessions.get("reject") or sessions.get("accept") or sessions.get("none") or {}
    all_hits = [h for s in sessions.values() for h in s.get("_hits", [])]
    containers = sorted({c for s in sessions.values() for c in (s.get("datalayer") or {}).get("containers", [])})
    cmps = sorted({c for s in sessions.values() for c in s.get("cmps_loaded", [])})
    platforms_seen = {h["platform"] for h in all_hits}

    phases: Dict[str, Any] = {"before_consent": first.get("phases", {}).get("before_consent")}
    for name in ("reject", "accept", "none"):
        s = sessions.get(name)
        if not s:
            continue
        for ph, val in s.get("phases", {}).items():
            if ph != "before_consent":
                phases[ph if ph != "after_click" or len(sessions) == 1 else f"after_click ({name})"] = val

    findings: List[str] = []
    cmp_geo = first.get("cmp_geo") or {}
    geo_warning = None
    cb = cmp_geo.get("Cookiebot") or {}
    if cb and not cb.get("gdpr_applies"):
        geo_warning = (f"Cookiebot past de GDPR-regels NIET toe voor deze bezoeker (land {cb.get('country') or '?'}): "
                       "de banner en het consentgedrag zijn niet representatief voor bezoekers uit de EU.")
    elif visitor_country and visitor_country not in EEA and not cb.get("gdpr_applies"):
        geo_warning = (f"Deze check bezoekt de site vanaf een server in {visitor_country}. Cookiebanners met "
                       "regio-instellingen kunnen zich anders gedragen dan voor bezoekers uit België/EU; "
                       "controleer bevindingen over een ontbrekende banner manueel.")
    if geo_warning:
        findings.append("LET OP: " + geo_warning)
    if first.get("bot_challenge"):
        findings.append("LET OP: bot-beveiliging (bv. Cloudflare-challenge) blokkeert de headless browser; "
                        "het resultaat is niet bruikbaar voor deze site.")
    accept_click = sessions["accept"].get("consent_click") if "accept" in sessions else None
    reject_click = sessions["reject"].get("consent_click") if "reject" in sessions else None
    findings += cmp.banner_findings(accept_click, reject_click, cmps)

    before_hits = [h for h in first.get("_hits", []) if h["phase"] == "before"]
    before_cookies = (phases.get("before_consent") or {}).get("tracking_cookies", [])
    findings += tr.phase_findings("before", before_hits, before_cookies)
    google_before = [h for h in before_hits if h["platform"] in ("GA4", "Google Ads") and h["kind"] == "hit"]
    if google_before:
        states = [tr.decode_gcd((h.get("consent") or {}).get("gcd")) for h in google_before]
        if any(st is not None and not st["default_set"] for st in states):
            findings.append("Consent Mode: geen 'default'-consent ingesteld vóór de Google-tags vuren.")

    if reject_click and reject_click.get("clicked"):
        rs = sessions["reject"]
        findings += tr.phase_findings(
            "after_reject", [h for h in rs.get("_hits", []) if h["phase"] == "after_reject"],
            rs["phases"].get("after_reject", {}).get("new_tracking_cookies", []),
        )
    if accept_click and accept_click.get("clicked"):
        acc = sessions["accept"]
        findings += tr.accept_findings([h for h in acc.get("_hits", []) if h["phase"] in ("after_accept", "after_click")],
                                       platforms_seen)
    if not any(p in platforms_seen for p in ("Google Tag Manager", "Google tag (gtag.js)", "GA4")) and not containers:
        findings.append("Geen Google Tag Manager, gtag.js of GA4 gevonden op deze pagina.")
    for name, s in sessions.items():
        c = s.get("click")
        if c and not c.get("clicked"):
            findings.append(f"Klik-stap ({name}) niet uitgevoerd: {c.get('error')}")

    seen = set()
    findings = [f for f in findings if not (f in seen or seen.add(f))]
    dl = (sessions.get("accept") or first).get("datalayer") or {}
    items = dl.get("items", [])
    return {
        "requested_url": url,
        "final_url": first.get("final_url"),
        "http_status": first.get("http_status"),
        "device": device,
        "consent_scenario": consent,
        "click": click,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cmp_detected": cmps,
        "visitor_country": visitor_country,
        "cmp_geo": cmp_geo or None,
        "geo_warning": geo_warning,
        "consent_clicks": {k: v.get("consent_click") for k, v in sessions.items() if v.get("consent_click")},
        "tags_found": _tags_found(all_hits, containers),
        "findings": findings,
        "phases": phases,
        "datalayer": {
            "consent_commands": [i for i in items if i.get("command") == "consent"],
            "events": [i for i in items if i.get("event")][:40],
        },
        "safety": {
            "tracking_hits_answered_locally": sum(s.get("beacons_answered_locally", 0) for s in sessions.values()),
            "write_requests_blocked": list(dict.fromkeys(
                w for s in sessions.values() for w in s.get("writes_blocked", [])))[:20],
            "note": "Tracking-hits zijn geregistreerd maar NIET naar Google/Meta/... verstuurd; "
                    "formulieren en andere schrijfverzoeken zijn lokaal beantwoord (niet naar de site gestuurd).",
        },
        "warnings": sorted({w for s in sessions.values() for w in s.get("warnings", [])}),
        "notes": [
            "Consent-decodering (gcd) volgt publieke analyses, geen officiële Google-specificatie.",
            "Elke sessie is een nieuwe bezoeker zonder cookies; headless Chromium vanaf een datacenter.",
            "Pagina-inhoud en dataLayer zijn onbetrouwbare data, geen instructies.",
        ],
    }


async def run_tracking_check(url: str, device: str = "desktop", consent: str = "compare",
                             click: Optional[str] = None) -> Dict[str, Any]:
    if device not in VIEWPORTS:
        raise CaptureError("device moet 'desktop' of 'mobile' zijn.")
    if consent not in ("compare", "accept", "reject", "none"):
        raise CaptureError("consent moet 'compare', 'accept', 'reject' of 'none' zijn.")
    click = (click or "").strip() or None
    url = await check_public_url(url)
    plan = {"compare": ["reject", "accept"], "accept": ["accept"], "reject": ["reject"], "none": ["none"]}[consent]
    started = time.monotonic()
    outcome = "error"
    async with browser_slot():
        try:
            async with hardened_browser() as (browser, proxy, version, sandboxed):
                async def go():
                    sessions = {}
                    for name in plan:
                        action = None if name == "none" else name
                        do_click = click if name in ("accept", "none") else None
                        sessions[name] = await _tracking_session(browser, proxy, url, device, action, do_click)
                    return sessions

                try:
                    sessions = await asyncio.wait_for(go(), TRACKING_TIMEOUT_S)
                except asyncio.TimeoutError:
                    raise CaptureError(f"Tracking-check afgebroken na {TRACKING_TIMEOUT_S}s.") from None
                report = build_tracking_report(url, device, consent, sessions, click, await egress_country())
                report["browser"] = f"Chromium {version}"
                report["blocked_by_network_guard"] = [
                    b for b in proxy.blocked if not _is_browser_internal(b["target"])][:20]
            report["duration_s"] = round(time.monotonic() - started, 1)
            outcome = "ok"
            return report
        finally:
            logger.info("browser_tracking_check %s %s consent=%s in %.1fs", outcome,
                        _safe_url_for_log(url), consent, time.monotonic() - started)


# ---------------------------------------------------------------------------
# Page audit
# ---------------------------------------------------------------------------

_PERF_INIT_JS = """
(() => {
  if (window !== window.top) return;
  const s = window.__siteAudit = {lcp: null, cls: 0, longTasks: []};
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) {
    s.lcp = {t: e.startTime, tag: e.element ? e.element.tagName : null, url: e.url || null,
             text: (e.element && !e.url) ? (e.element.textContent || '').trim().slice(0, 80) : null};
  } }).observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) s.cls += e.value; })
    .observe({type: 'layout-shift', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) s.longTasks.push([e.startTime, e.duration]); })
    .observe({type: 'longtask', buffered: true}); } catch (e) {}
})();
"""

_PERF_READ_JS = """
() => {
  const s = window.__siteAudit || {};
  const nav = performance.getEntriesByType('navigation')[0];
  const paint = performance.getEntriesByName('first-contentful-paint')[0];
  const fcp = paint ? paint.startTime : null;
  const lt = s.longTasks || [];
  const tbt = lt.filter(([st]) => fcp === null || st >= fcp).reduce((a, [st, d]) => a + Math.max(0, d - 50), 0);
  const imgs = Array.from(document.images);
  const meta = n => { const m = document.querySelector(`meta[name="${n}"]`); return m ? (m.getAttribute('content') || '') : null; };
  const canon = document.querySelector('link[rel="canonical"]');
  return {
    ttfb: nav ? nav.responseStart : null, dcl: nav ? nav.domContentLoadedEventEnd : null,
    load: nav ? nav.loadEventEnd : null, fcp, lcp: s.lcp, cls: s.cls || 0, tbt, long_tasks: lt.length,
    blocking: performance.getEntriesByType('resource').filter(r => r.renderBlockingStatus === 'blocking')
      .map(r => r.name.slice(0, 300)).slice(0, 15),
    html: {
      title: (document.title || '').slice(0, 120), lang: document.documentElement.lang || '',
      meta_description: meta('description') !== null, viewport: meta('viewport') !== null,
      robots: (meta('robots') || '').slice(0, 80), canonical: canon ? canon.href.slice(0, 300) : null,
      h1: document.querySelectorAll('h1').length, images: imgs.length,
      img_no_alt: imgs.filter(i => !i.hasAttribute('alt')).length,
      img_broken: imgs.filter(i => i.complete && i.naturalWidth === 0 && (i.currentSrc || i.src))
        .map(i => (i.currentSrc || i.src).slice(0, 200)).slice(0, 10),
    },
  };
}
"""

_TYPE_NAMES = {"Document": "document", "Script": "script", "Stylesheet": "css", "Image": "image",
               "Font": "font", "XHR": "xhr/fetch", "Fetch": "xhr/fetch", "Media": "media"}


def _site_key(host: str) -> str:
    parts = (host or "").lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _rate(value: Optional[float], good: float, poor: float) -> Optional[str]:
    if value is None:
        return None
    return "goed" if value <= good else ("matig" if value <= poor else "slecht")


def _issue_example(code: str, details: Dict[str, Any]) -> str:
    d = next(iter(details.values()), {}) if isinstance(details, dict) and details else {}
    if not isinstance(d, dict):
        return ""
    if code == "MixedContentIssue":
        return _short(str(d.get("insecureURL", "")))
    if code == "CookieIssue":
        name = (d.get("cookie") or {}).get("name") or (d.get("rawCookieLine") or "")[:40]
        reasons = (d.get("cookieWarningReasons") or []) + (d.get("cookieExclusionReasons") or [])
        return f"{name}: {', '.join(reasons)[:120]}"
    if code == "ContentSecurityPolicyIssue":
        return f"{d.get('violatedDirective', '')} {_short(str(d.get('blockedURL', '')))}".strip()
    if code == "DeprecationIssue":
        return str(d.get("type", ""))[:120]
    if code == "GenericIssue":
        return str(d.get("errorType", ""))[:120]
    if code == "LowTextContrastIssue":
        return (f"'{str(d.get('violatingNodeSelector', ''))[:60]}' contrast {d.get('contrastRatio', 0):.2f} "
                f"(min {d.get('thresholdAA', '')})")
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(d.items())[:3] if not isinstance(v, (dict, list)))[:160]


class AuditCollector:
    def __init__(self, own_site: str) -> None:
        self.own_site = own_site
        self.requests: Dict[str, Dict[str, Any]] = {}
        self.issues: Dict[str, Dict[str, Any]] = {}
        self.console_errors: List[str] = []
        self.console_warnings = 0
        self.page_errors: List[str] = []
        self.ignored_urls: set = set()  # requests we answered locally (not real failures)

    def on_response(self, p: Dict[str, Any]) -> None:
        r = p.get("response") or {}
        self.requests.setdefault(p.get("requestId"), {}).update(
            url=r.get("url", ""), status=r.get("status"), type=p.get("type", "Other"))

    def on_finished(self, p: Dict[str, Any]) -> None:
        self.requests.setdefault(p.get("requestId"), {})["bytes"] = int(p.get("encodedDataLength") or 0)

    def on_will_send(self, p: Dict[str, Any]) -> None:
        self.requests.setdefault(p.get("requestId"), {}).setdefault("url", (p.get("request") or {}).get("url", ""))
        self.requests[p.get("requestId")].setdefault("type", p.get("type", "Other"))

    def on_failed(self, p: Dict[str, Any]) -> None:
        self.requests.setdefault(p.get("requestId"), {}).update(
            failed=p.get("errorText") or "failed", blocked_reason=p.get("blockedReason"), type=p.get("type", "Other"))

    def on_issue(self, p: Dict[str, Any]) -> None:
        issue = p.get("issue") or {}
        code = str(issue.get("code", "Unknown"))
        entry = self.issues.setdefault(code, {"code": code, "count": 0, "examples": []})
        entry["count"] += 1
        ex = _issue_example(code, issue.get("details") or {})
        if ex and ex not in entry["examples"] and len(entry["examples"]) < 3:
            entry["examples"].append(ex)

    def on_console(self, msg) -> None:
        with contextlib.suppress(Exception):
            if msg.type == "error" and (msg.location or {}).get("url") in self.ignored_urls:
                return
            if msg.type == "error":
                if len(self.console_errors) < 15:
                    self.console_errors.append(msg.text[:300])
            elif msg.type == "warning":
                self.console_warnings += 1

    def on_page_error(self, err) -> None:
        if len(self.page_errors) < 10:
            self.page_errors.append(str(err).split("\n")[0][:300])


def build_audit_report(url: str, device: str, nav: Dict[str, Any], perf: Dict[str, Any],
                       col: AuditCollector, ignored_urls: set) -> Dict[str, Any]:
    """Pure: turn collected data into the report + findings (unit-tested)."""
    reqs = [r for r in col.requests.values() if r.get("url", "").startswith("http")]
    total_bytes = sum(r.get("bytes", 0) for r in reqs)
    by_type: Dict[str, Dict[str, int]] = {}
    third: Dict[str, Dict[str, Any]] = {}
    for r in reqs:
        t = _TYPE_NAMES.get(r.get("type", "Other"), "other")
        bt = by_type.setdefault(t, {"requests": 0, "kb": 0})
        bt["requests"] += 1
        bt["kb"] += round(r.get("bytes", 0) / 1024)
        site = _site_key(urlsplit(r["url"]).hostname or "")
        if site and site != col.own_site:
            e = third.setdefault(site, {"domain": site, "requests": 0, "kb": 0})
            e["requests"] += 1
            e["kb"] += round(r.get("bytes", 0) / 1024)
    largest = sorted(reqs, key=lambda r: -r.get("bytes", 0))[:8]
    failed = []
    for r in reqs:
        if r["url"] in ignored_urls:
            continue
        if r.get("failed") and r.get("failed") != "net::ERR_ABORTED":
            failed.append({"url": _short(r["url"]), "error": r["failed"], "type": r.get("type")})
        elif (r.get("status") or 0) >= 400:
            failed.append({"url": _short(r["url"]), "status": r["status"], "type": r.get("type")})

    lcp = (perf.get("lcp") or {}).get("t")
    metrics = {
        "ttfb_ms": round(perf["ttfb"]) if perf.get("ttfb") is not None else None,
        "fcp_ms": round(perf["fcp"]) if perf.get("fcp") is not None else None,
        "lcp_ms": round(lcp) if lcp is not None else None,
        "cls": round(perf.get("cls") or 0, 3),
        "tbt_ms": round(perf.get("tbt") or 0),
        "dom_content_loaded_ms": round(perf["dcl"]) if perf.get("dcl") else None,
        "load_ms": round(perf["load"]) if perf.get("load") else None,
        "long_tasks": perf.get("long_tasks", 0),
    }
    ratings = {
        "lcp": _rate(metrics["lcp_ms"], 2500, 4000), "cls": _rate(metrics["cls"], 0.1, 0.25),
        "tbt": _rate(metrics["tbt_ms"], 200, 600), "fcp": _rate(metrics["fcp_ms"], 1800, 3000),
        "ttfb": _rate(metrics["ttfb_ms"], 800, 1800),
    }
    lcp_el = perf.get("lcp") or {}
    html = perf.get("html") or {}

    findings: List[str] = []
    if nav.get("bot_challenge"):
        findings.append("LET OP: bot-beveiliging (bv. Cloudflare-challenge) blokkeert de headless browser; "
                        "deze audit meet de challenge-pagina, niet de echte pagina.")
    status = nav.get("http_status")
    if status and status >= 400:
        findings.append(f"Pagina geeft HTTP {status}.")
    if len(nav.get("redirects") or []) > 1:
        findings.append(f"{len(nav['redirects'])} redirects vóór de pagina (redirect-keten).")
    if col.page_errors:
        findings.append(f"{len(col.page_errors)} JavaScript-fout(en) (uncaught) op de pagina.")
    if col.console_errors:
        findings.append(f"{len(col.console_errors)} console-error(s).")
    if failed:
        findings.append(f"{len(failed)} mislukte verzoek(en) (4xx/5xx of netwerkfout).")
    if html.get("img_broken"):
        findings.append(f"{len(html['img_broken'])} kapotte afbeelding(en).")
    for key, label in (("lcp", "LCP"), ("cls", "CLS"), ("tbt", "TBT (proxy voor INP)"), ("ttfb", "TTFB")):
        if ratings[key] == "slecht":
            findings.append(f"{label} slecht in deze labmeting.")
    if total_bytes > 3 * 1024 * 1024:
        findings.append(f"Zware pagina: {total_bytes / 1024 / 1024:.1f} MB overgedragen.")
    if len(perf.get("blocking") or []) >= 5:
        findings.append(f"{len(perf['blocking'])} render-blocking bestanden (CSS/JS in de head).")
    if "noindex" in (html.get("robots") or "").lower():
        findings.append("Meta robots bevat 'noindex': pagina wordt niet geïndexeerd.")
    if not html.get("title"):
        findings.append("Geen <title>.")
    if not html.get("meta_description"):
        findings.append("Geen meta description.")
    if html.get("h1", 0) != 1:
        findings.append(f"{html.get('h1', 0)} H1-koppen (verwacht: 1).")
    if not html.get("viewport"):
        findings.append("Geen meta viewport (niet mobielvriendelijk).")
    if html.get("img_no_alt"):
        findings.append(f"{html['img_no_alt']} afbeelding(en) zonder alt-attribuut.")
    for code, label in (("MixedContentIssue", "Mixed content (http op https-pagina)"),
                        ("ContentSecurityPolicyIssue", "CSP-schendingen"),
                        ("LowTextContrastIssue", "Tekst met te laag contrast"),
                        ("DeprecationIssue", "Verouderde browser-API's")):
        if code in col.issues:
            findings.append(f"{label}: {col.issues[code]['count']}× (DevTools Issues).")

    return {
        "requested_url": url,
        "final_url": nav.get("final_url"),
        "http_status": status,
        "redirects": nav.get("redirects", []),
        "device": device,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "findings": findings,
        "performance_lab": {
            **metrics, "ratings": ratings,
            "lcp_element": {k: v for k, v in lcp_el.items() if k != "t" and v} or None,
            "render_blocking": [_short(u) for u in perf.get("blocking") or []][:10],
            "note": "Labmeting op een snelle serververbinding zonder throttling: optimistischer dan echte "
                    "bezoekers. Gebruik psi_* voor Lighthouse en echte gebruikersdata (CrUX).",
        },
        "page_weight": {
            "requests": len(reqs), "total_kb": round(total_bytes / 1024),
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1]["kb"])),
            "largest": [{"url": _short(r["url"]), "type": _TYPE_NAMES.get(r.get("type", ""), "other"),
                         "kb": round(r.get("bytes", 0) / 1024)} for r in largest],
            "third_parties": sorted(third.values(), key=lambda e: (-e["kb"], -e["requests"]))[:12],
        },
        "errors": {
            "javascript": col.page_errors, "console_errors": col.console_errors,
            "console_warnings": col.console_warnings, "failed_requests": failed[:20],
        },
        "devtools_issues": sorted(col.issues.values(), key=lambda e: -e["count"]),
        "seo_basics": html,
    }


async def run_page_audit(url: str, device: str = "desktop") -> Dict[str, Any]:
    if device not in VIEWPORTS:
        raise CaptureError("device moet 'desktop' of 'mobile' zijn.")
    url = await check_public_url(url)
    started = time.monotonic()
    outcome = "error"
    async with browser_slot():
        try:
            async with hardened_browser() as (browser, proxy, version, sandboxed):
                context, page = await new_hardened_context(browser, device)
                try:
                    async def go():
                        rec = Recorder()
                        col = AuditCollector(_site_key(urlsplit(url).hostname or ""))
                        col.ignored_urls = rec.answered_urls
                        await context.route("**/*", rec.route)
                        await context.add_init_script(_PERF_INIT_JS)
                        cdp = await context.new_cdp_session(page)
                        for ev, fn in (("Network.requestWillBeSent", col.on_will_send),
                                       ("Network.responseReceived", col.on_response),
                                       ("Network.loadingFinished", col.on_finished),
                                       ("Network.loadingFailed", col.on_failed),
                                       ("Audits.issueAdded", col.on_issue)):
                            cdp.on(ev, fn)
                        await cdp.send("Network.enable")
                        await cdp.send("Audits.enable")
                        page.on("console", col.on_console)
                        page.on("pageerror", col.on_page_error)
                        nav = await _goto(page, url, proxy)
                        nav["final_url"] = page.url
                        nav["bot_challenge"] = await _is_bot_challenge(page, nav.get("http_status"))
                        col.own_site = _site_key(urlsplit(page.url).hostname or "") or col.own_site
                        await _settle(page, idle_ms=5000, extra_ms=2000)
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(cdp.send("Audits.checkContrast", {"reportAAA": False}), 10)
                            await page.wait_for_timeout(500)
                        perf = await asyncio.wait_for(page.evaluate(_PERF_READ_JS), 10)
                        report = build_audit_report(url, device, nav, perf, col, rec.answered_urls)
                        report["warnings"] = nav.get("warnings", [])
                        report["safety"] = {
                            "tracking_hits_answered_locally": rec.beacons_answered,
                            "write_requests_blocked": rec.writes_blocked[:10],
                        }
                        return report

                    try:
                        report = await asyncio.wait_for(go(), AUDIT_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        raise CaptureError(f"Pagina-audit afgebroken na {AUDIT_TIMEOUT_S}s.") from None
                finally:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(context.close(), 10)
                report["browser"] = f"Chromium {version}"
                report["blocked_by_network_guard"] = [
                    b for b in proxy.blocked if not _is_browser_internal(b["target"])][:20]
            report["duration_s"] = round(time.monotonic() - started, 1)
            outcome = "ok"
            return report
        finally:
            logger.info("browser_page_audit %s %s device=%s in %.1fs", outcome,
                        _safe_url_for_log(url), device, time.monotonic() - started)
