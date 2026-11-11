"""Pure helpers for the tracking/consent check: recognise tracking requests,
decode Google Consent Mode parameters, classify cookies and derive findings.

No browser code here, so everything is unit-testable.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlsplit

# ---------------------------------------------------------------------------
# Request classification
# ---------------------------------------------------------------------------

GOOGLE_PLATFORMS = ("GA4", "Google Ads", "Floodlight")
# Platforms that set marketing/analytics cookies or send personal data without
# a consent-aware fallback (unlike Google tags in Consent Mode).
NON_GOOGLE_TRACKERS = (
    "Meta Pixel", "LinkedIn Insight", "TikTok Pixel", "Microsoft Ads (UET)", "Microsoft Clarity",
    "Hotjar", "Pinterest Tag", "Snapchat Pixel", "X (Twitter) Pixel",
)


def _params(url: str, post_data: Optional[str]) -> List[Dict[str, str]]:
    """Query params + (for batched beacons) one param dict per body line."""
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if not post_data:
        return [query]
    body = post_data.strip()
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            return [query]
        return [{**query, "_json": data}] if isinstance(data, dict) else [query]
    lines = [ln for ln in body.splitlines() if "=" in ln]
    if not lines:
        return [query]
    return [{**query, **dict(parse_qsl(ln, keep_blank_values=True))} for ln in lines]


def _path_id(path: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, path)
    return m.group(1) if m else None


def classify_request(url: str, method: str = "GET", post_data: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns zero or more tracking 'hits' for one request.

    Each hit: {platform, kind ('hit' | 'library'), id, event, consent{gcs,gcd}, beacon(bool)}.
    ``beacon`` = a data-sending request (safe to answer locally instead of sending it out).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    host = (parts.hostname or "").lower()
    path = parts.path or "/"

    def hit(platform: str, kind: str = "hit", id_: Optional[str] = None, event: Optional[str] = None,
            p: Optional[Dict] = None, beacon: bool = True) -> Dict[str, Any]:
        p = p or {}
        out = {"platform": platform, "kind": kind, "id": id_, "event": event, "beacon": beacon}
        consent = {k: p.get(k) for k in ("gcs", "gcd") if p.get(k)}
        if consent:
            out["consent"] = consent
        return out

    # --- Google tag loaders -------------------------------------------------
    if path.endswith("/gtm.js") or path.endswith("/gtag/js"):
        q = dict(parse_qsl(parts.query))
        tid = q.get("id")
        if tid:
            platform = "Google Tag Manager" if tid.startswith("GTM-") else "Google tag (gtag.js)"
            return [hit(platform, "library", tid, beacon=False)]
        return []

    # --- GA4 (also server-side GTM on a custom domain) ----------------------
    if re.search(r"/g/collect$", path) or (host.endswith("google-analytics.com") and path.endswith("/collect")):
        rows = _params(url, post_data)
        return [hit("GA4", "hit", p.get("tid"), p.get("en"), p) for p in rows if p.get("tid") or p.get("en")]

    # --- Google Ads ----------------------------------------------------------
    if host.endswith("googleadservices.com") and "/pagead/conversion/" in path:
        p = _params(url, post_data)[0]
        aw = _path_id(path, r"/pagead/conversion/(\d+)")
        return [hit("Google Ads", "hit", f"AW-{aw}" if aw else None, "conversion" + (f" ({p['label']})" if p.get("label") else ""), p)]
    if host in ("www.google.com", "google.com") or host.startswith("www.google."):
        if "/pagead/1p-conversion/" in path:
            p = _params(url, post_data)[0]
            aw = _path_id(path, r"/pagead/1p-conversion/(\d+)")
            return [hit("Google Ads", "hit", f"AW-{aw}" if aw else None, "conversion" + (f" ({p['label']})" if p.get("label") else ""), p)]
        if "/pagead/1p-user-list/" in path:
            p = _params(url, post_data)[0]
            aw = _path_id(path, r"/pagead/1p-user-list/(\d+)")
            return [hit("Google Ads", "hit", f"AW-{aw}" if aw else None, "remarketing", p)]
        if path.startswith("/rmkt/collect/"):
            p = _params(url, post_data)[0]
            aw = _path_id(path, r"/rmkt/collect/(\d+)")
            return [hit("Google Ads", "hit", f"AW-{aw}" if aw else None, "remarketing", p)]
        if path.startswith("/ccm/collect"):
            p = _params(url, post_data)[0]
            return [hit("Google Ads", "hit", p.get("tid"), p.get("en") or "ccm", p)]
    if host.endswith("doubleclick.net"):
        if "/pagead/viewthroughconversion/" in path:
            p = _params(url, post_data)[0]
            aw = _path_id(path, r"/pagead/viewthroughconversion/(\d+)")
            return [hit("Google Ads", "hit", f"AW-{aw}" if aw else None, "remarketing", p)]
        if path.startswith("/ccm/s/collect") or path.startswith("/ccm/collect"):
            p = _params(url, post_data)[0]
            return [hit("Google Ads", "hit", p.get("tid"), p.get("en") or "ccm", p)]
        if host.startswith("fls.") or "/activity" in path or "/activityi" in path:
            p = _params(url, post_data)[0]
            src = re.search(r"src=(\d+)", url)
            return [hit("Floodlight", "hit", src.group(1) if src else None, None, p)]
        if host == "td.doubleclick.net" or host == "stats.g.doubleclick.net":
            return []

    # --- Meta ----------------------------------------------------------------
    if host.endswith("facebook.com") and path.rstrip("/") == "/tr":
        p = _params(url, post_data)[0]
        return [hit("Meta Pixel", "hit", p.get("id"), p.get("ev"), p)]
    if host == "connect.facebook.net" and "fbevents.js" in path:
        return [hit("Meta Pixel", "library", _path_id(path, r"/signals/config/(\d+)"), beacon=False)]

    # --- Others ---------------------------------------------------------------
    if host == "px.ads.linkedin.com" or host.endswith(".ads.linkedin.com"):
        p = _params(url, post_data)[0]
        return [hit("LinkedIn Insight", "hit", p.get("pid"), p.get("conversionId") and f"conversion {p['conversionId']}" or "pageview", p)]
    if host == "snap.licdn.com":
        return [hit("LinkedIn Insight", "library", beacon=False)]
    if host == "analytics.tiktok.com":
        if "/api/" in path:
            data = (_params(url, post_data)[0]).get("_json") or {}
            pixel = ((data.get("context") or {}).get("pixel") or {}).get("code") if isinstance(data, dict) else None
            return [hit("TikTok Pixel", "hit", pixel, data.get("event") if isinstance(data, dict) else None)]
        return [hit("TikTok Pixel", "library", dict(parse_qsl(parts.query)).get("sdkid"), beacon=False)]
    if host == "bat.bing.com" or host == "bat.bing.net":
        if path.startswith("/action"):
            p = _params(url, post_data)[0]
            return [hit("Microsoft Ads (UET)", "hit", p.get("ti"), p.get("ea") or p.get("evt"), p)]
        return [hit("Microsoft Ads (UET)", "library", beacon=False)]
    if host.endswith("clarity.ms"):
        if path.startswith("/tag/"):
            return [hit("Microsoft Clarity", "library", path.split("/")[2] or None, beacon=False)]
        if path.startswith("/collect"):
            return [hit("Microsoft Clarity", "hit", None, "collect")]
        return []
    if host == "static.hotjar.com":
        return [hit("Hotjar", "library", _path_id(path, r"hotjar-(\d+)\.js"), beacon=False)]
    if host.endswith(".hotjar.com") or host.endswith(".hotjar.io"):
        return [hit("Hotjar", "hit", None, "collect")] if host not in ("script.hotjar.com", "vars.hotjar.com") else []
    if host == "ct.pinterest.com":
        p = _params(url, post_data)[0]
        return [hit("Pinterest Tag", "hit", p.get("tid"), p.get("event"), p)]
    if host == "tr.snapchat.com":
        return [hit("Snapchat Pixel", "hit")]
    if host in ("analytics.twitter.com", "t.co") and path.startswith(("/i/adsct", "/1/i/adsct")):
        p = _params(url, post_data)[0]
        return [hit("X (Twitter) Pixel", "hit", p.get("txn_id"), p.get("events"), p)]
    return []


def is_beacon(url: str, method: str = "GET", post_data: Optional[str] = None) -> bool:
    return any(h.get("beacon") for h in classify_request(url, method, post_data))


# ---------------------------------------------------------------------------
# Consent Mode decoding
# ---------------------------------------------------------------------------

_GCS = {"1": "granted", "0": "denied", "-": "not set"}
# gcd letters (community-documented, not an official Google spec):
_GCD_STATE = {
    "l": ("not set", False), "p": ("denied", True), "q": ("denied", True), "t": ("granted", True),
    "r": ("granted", True), "m": ("denied", False), "n": ("granted", False), "u": ("denied", True),
    "v": ("granted", True), "e": ("denied", False), "f": ("granted", False),
}
GCD_SIGNALS = ("ad_storage", "analytics_storage", "ad_user_data", "ad_personalization")


def decode_gcs(gcs: Optional[str]) -> Optional[Dict[str, str]]:
    if not gcs or not re.fullmatch(r"G1[01\-]{2}", gcs):
        return None
    return {"ad_storage": _GCS[gcs[2]], "analytics_storage": _GCS[gcs[3]]}


def decode_gcd(gcd: Optional[str]) -> Optional[Dict[str, Any]]:
    if not gcd:
        return None
    letters = re.findall(r"[a-z]", gcd)
    if len(letters) < 4:
        return None
    out: Dict[str, Any] = {}
    default_set = False
    for name, letter in zip(GCD_SIGNALS, letters[:4]):
        state, has_default = _GCD_STATE.get(letter, ("unknown", False))
        out[name] = state
        default_set = default_set or has_default
    out["default_set"] = default_set
    return out


def consent_state(consent: Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Merged view from gcs/gcd (gcd wins when both present)."""
    if not consent:
        return None
    state = decode_gcd(consent.get("gcd")) or {}
    gcs = decode_gcs(consent.get("gcs"))
    if gcs:
        for k, v in gcs.items():
            state.setdefault(k, v)
    return state or None


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

_COOKIE_RULES = (
    (r"^_ga($|_)|^_gid$|^_gat", "analytics", "Google Analytics"),
    (r"^_gcl_|^_gac_|^FPAU$|^FPGCLAW$", "marketing", "Google Ads"),
    (r"^_fbp$|^_fbc$|^fr$", "marketing", "Meta"),
    (r"^_uet", "marketing", "Microsoft Ads"),
    (r"^_clck$|^_clsk$|^CLID$|^MUID$", "analytics", "Microsoft Clarity/Bing"),
    (r"^_hj", "analytics", "Hotjar"),
    (r"^_ttp$|^_tt_enable_cookie$", "marketing", "TikTok"),
    (r"^_pin_unauth$|^_pinterest_", "marketing", "Pinterest"),
    (r"^li_|^lidc$|^bcookie$|^UserMatchHistory$|^AnalyticsSyncHistory$", "marketing", "LinkedIn"),
    (r"^_scid", "marketing", "Snapchat"),
    (r"^IDE$|^test_cookie$|^DSID$", "marketing", "Google (doubleclick)"),
)


def classify_cookie(name: str) -> Optional[Dict[str, str]]:
    for pattern, category, vendor in _COOKIE_RULES:
        if re.search(pattern, name):
            return {"category": category, "vendor": vendor}
    return None


def tracking_cookies(cookies: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Only analytics/marketing cookies; names + domain, never values."""
    out = []
    seen = set()
    for c in cookies:
        info = classify_cookie(str(c.get("name", "")))
        key = (c.get("name"), c.get("domain"))
        if info and key not in seen:
            seen.add(key)
            out.append({"name": c.get("name"), "domain": c.get("domain"), **info})
    return out


# ---------------------------------------------------------------------------
# Summaries & findings
# ---------------------------------------------------------------------------


def summarize_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate per platform: ids, event counts, consent states, hit/library counts."""
    by: Dict[str, Dict[str, Any]] = {}
    for h in hits:
        s = by.setdefault(h["platform"], {"platform": h["platform"], "ids": [], "events": {},
                                           "hits": 0, "library_loaded": False, "consent": []})
        if h.get("id") and h["id"] not in s["ids"]:
            s["ids"].append(h["id"])
        if h["kind"] == "library":
            s["library_loaded"] = True
            continue
        s["hits"] += 1
        if h.get("event"):
            s["events"][h["event"]] = s["events"].get(h["event"], 0) + 1
        state = consent_state(h.get("consent"))
        if state and state not in s["consent"]:
            s["consent"].append(state)
    out = []
    for s in by.values():
        if not s["consent"]:
            s.pop("consent")
        out.append(s)
    return sorted(out, key=lambda s: (-s["hits"], s["platform"]))


def _granted_any(state: Dict[str, Any]) -> bool:
    return any(state.get(k) == "granted" for k in GCD_SIGNALS)


def phase_findings(phase: str, hits: List[Dict[str, Any]], cookies: List[Dict[str, str]]) -> List[str]:
    """Findings for a phase where the visitor has NOT (yet) given consent ('before' / 'after_reject')."""
    label = "vóór een keuze in de cookiebanner" if phase == "before" else "na WEIGEREN"
    issues: List[str] = []
    for s in summarize_hits([h for h in hits if h["kind"] == "hit"]):
        if s["platform"] in NON_GOOGLE_TRACKERS:
            ev = ", ".join(list(s["events"])[:5])
            issues.append(f"{s['platform']} verstuurt data {label}" + (f" ({ev})" if ev else "") + ".")
        elif s["platform"] in GOOGLE_PLATFORMS:
            states = s.get("consent") or []
            if not states:
                issues.append(f"{s['platform']}-hits {label} zonder Consent Mode-signalen (gcs/gcd ontbreken).")
            elif any(_granted_any(st) for st in states):
                issues.append(f"{s['platform']}-hits {label} met consent 'granted'.")
    for c in cookies:
        issues.append(f"Cookie {c['name']} ({c['vendor']}, {c['category']}) gezet {label}.")
    return issues


def accept_findings(hits: List[Dict[str, Any]], platforms_seen: Iterable[str]) -> List[str]:
    issues: List[str] = []
    summ = {s["platform"]: s for s in summarize_hits([h for h in hits if h["kind"] == "hit"])}
    for name in ("GA4", "Google Ads"):
        s = summ.get(name)
        if not s:
            continue
        states = s.get("consent") or []
        if states and not any(_granted_any(st) for st in states):
            issues.append(f"{name}: na ACCEPTEREN blijft consent 'denied' (consent-update komt niet door).")
        for st in states:
            missing = [k for k in ("ad_user_data", "ad_personalization") if st.get(k) == "not set"]
            if missing:
                issues.append(f"{name}: Consent Mode v2-signalen ontbreken ({', '.join(missing)}); "
                              "nodig voor Google Ads in de EER.")
                break
    if "GA4" in platforms_seen and "GA4" not in summ:
        issues.append("GA4-tag aanwezig maar na ACCEPTEREN geen GA4-hit gezien.")
    return issues


def compact_datalayer(items: Any, limit: int = 60) -> List[Any]:
    """dataLayer entries from the page (untrusted): events/consent commands only, truncated."""
    out: List[Any] = []
    if not isinstance(items, list):
        return out
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        if it.get("_cmd"):
            entry = {"command": str(it.get("_cmd"))[:40], "arg": str(it.get("arg") or "")[:60]}
            if isinstance(it.get("params"), dict):
                entry["params"] = {str(k)[:40]: str(v)[:40] for k, v in list(it["params"].items())[:12]}
            out.append(entry)
        elif it.get("event"):
            keys = [str(k)[:40] for k in (it.get("keys") or [])][:15]
            out.append({"event": str(it["event"])[:80], "keys": keys})
    return out
