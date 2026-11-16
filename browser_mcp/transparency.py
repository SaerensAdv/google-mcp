"""Read-only Google Ads Transparency Center research.

This uses the public Transparency Center UI, not private Google Ads data.
It returns public advertiser/ad metadata and preview URLs; it never signs in,
clicks an ad, or follows an advertiser's landing page.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from browser_mcp.capture import (
    CaptureError,
    browser_slot,
    hardened_browser,
    new_hardened_context,
)

BASE_URL = "https://adstransparency.google.com/"
MAX_ADS = 30
WAIT_MS = 2500


def normalize_region(region: str) -> str:
    value = (region or "anywhere").strip().lower()
    if value == "anywhere":
        return value
    if re.fullmatch(r"[a-z]{2}", value):
        return value.upper()
    raise CaptureError("region moet 'anywhere' of een ISO-landcode van 2 letters zijn.")


def _domain_from_query(query: str) -> Optional[str]:
    raw = (query or "").strip()
    if not raw or " " in raw:
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "." not in host or "." not in raw:
        return None
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for ch in host):
        return None
    return host


def build_transparency_url(query: str, region: str = "anywhere") -> str:
    """Builds a public domain-search URL when query looks like a website."""
    normalized_region = normalize_region(region)
    domain = _domain_from_query(query)
    if not domain:
        return f"{BASE_URL}?region={quote(normalized_region, safe='')}"
    return f"{BASE_URL}?domain={quote(domain, safe='')}&region={quote(normalized_region, safe='')}"


def parse_ad_count(text: str) -> Optional[int]:
    """Extracts the visible ad count from Dutch or English UI text."""
    match = re.search(r"(?<!\d)([\d.,]+)\s+(?:advertenties|ads)\b", text or "", re.I)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    return int(digits) if digits else None


def parse_detail_labels(text: str) -> Dict[str, Optional[str]]:
    """Extracts stable labels from an ad detail page without copying ad text."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()]
    out: Dict[str, Optional[str]] = {
        "last_shown": None,
        "format": None,
        "shown_in": None,
    }
    labels = {
        "last_shown": ("Voor het laatst weergegeven", "Last shown"),
        "format": ("Indeling", "Format"),
        "shown_in": ("Getoond in", "Shown in"),
    }
    for key, candidates in labels.items():
        for index, line in enumerate(lines):
            for label in candidates:
                if line.lower().startswith(label.lower() + ":"):
                    out[key] = line.split(":", 1)[1].strip() or None
                elif line.lower() == label.lower() and index + 1 < len(lines):
                    out[key] = lines[index + 1] or None
    return out


def _card_format(class_name: str) -> str:
    classes = (class_name or "").lower()
    if "wide" in classes:
        return "wide"
    if "tall" in classes:
        return "tall"
    return "other"


def _dedupe_cards(cards: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for card in cards:
        key = card.get("creative_url") or card.get("image_url")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(card)
        if len(out) >= limit:
            break
    return out


async def _wait_for_results(page) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=8000)
    await page.wait_for_timeout(WAIT_MS)


async def _search_page(page, query: str, region: str) -> str:
    domain = _domain_from_query(query)
    if domain:
        await page.goto(build_transparency_url(domain, region), wait_until="domcontentloaded", timeout=60000)
        await _wait_for_results(page)
        return "domain"

    await page.goto(build_transparency_url("", region), wait_until="domcontentloaded", timeout=60000)
    await _wait_for_results(page)
    inputs = page.locator("input.input-area")
    if not await inputs.count():
        raise CaptureError("Google Ads Transparency Center toont geen zoekveld.")
    await inputs.first.fill(query.strip()[:200])
    await inputs.first.press("Enter")
    await _wait_for_results(page)
    options = page.locator('material-select-item[role="option"]')
    if await options.count():
        await options.first.click()
        await _wait_for_results(page)
    return "advertiser"


async def _extract_cards(page, limit: int) -> List[Dict[str, Any]]:
    # A small scroll helps advertiser pages load more creative previews without
    # turning this into a full-page crawl.
    for _ in range(4):
        if await page.locator("creative-preview").count() >= limit:
            break
        with contextlib.suppress(Exception):
            await page.mouse.wheel(0, 1100)
        await page.wait_for_timeout(700)

    raw = await page.locator("creative-preview").evaluate_all(
        """els => els.map((e, i) => {
          const a = e.querySelector('a[href*="/creative/"]');
          const box = e.querySelector('.creative-bounding-box');
          const ad = e.querySelector('creative[aria-label]') || e.querySelector('[aria-label^="Advertentie"]');
          const img = e.querySelector('img[src*="simgad"]');
          const name = e.querySelector('.advertiser-name, .name');
          const verified = e.querySelector('.verified, .disclaimer');
          const adv = e.querySelector('.thumbnail-container');
          const absolutize = href => href ? new URL(href, location.href).href : null;
          return {
            position: i + 1,
            advertiser: name ? (name.innerText || '').trim() : null,
            verified: !!verified && /verified|geverifieerd/i.test(verified.innerText || ''),
            advertiser_url: adv ? absolutize(adv.getAttribute('href')) : null,
            creative_url: a ? a.href : null,
            image_url: img ? img.src : null,
            format: box ? ((box.className || '').toString()) : '',
            label: ad ? (ad.getAttribute('aria-label') || '') : (a ? (a.getAttribute('aria-label') || '') : '')
          };
        })"""
    )
    cards = []
    for item in raw or []:
        if not item.get("creative_url"):
            continue
        cards.append({
            "position": item.get("position"),
            "advertiser": item.get("advertiser"),
            "verified": bool(item.get("verified")),
            "advertiser_url": item.get("advertiser_url") or None,
            "creative_url": item.get("creative_url"),
            "preview_image_url": item.get("image_url"),
            "format": _card_format(item.get("format", "")),
            "label": item.get("label") or None,
        })
    return _dedupe_cards(cards, limit)


async def _page_info(page, query: str, query_type: str, region: str) -> Dict[str, Any]:
    text = await page.locator("body").inner_text()
    cards = await _extract_cards(page, 30)
    headings = [line.strip() for line in text.splitlines() if line.strip()]
    advertiser = next(
        (c["advertiser"] for c in cards if c.get("advertiser")),
        None,
    )
    if not advertiser and query_type == "advertiser":
        for line in headings:
            if line.lower() not in {"advertiser details", "advertentiedetails"} and len(line) < 160:
                if "Transparency" not in line and "Centrum" not in line:
                    advertiser = line
                    break
    return {
        "advertiser": advertiser,
        "ad_count_visible": parse_ad_count(text),
        "cards": cards,
        "page_title": await page.title(),
        "final_url": page.url,
        "query_type": query_type,
        "query": query,
        "region": region,
    }


def build_transparency_report(
    query: str,
    region: str,
    query_type: str,
    page_info: Dict[str, Any],
    duration_s: float,
) -> Dict[str, Any]:
    cards = page_info.get("cards") or []
    return {
        "requested_query": query,
        "query_type": query_type,
        "region": region,
        "advertiser": page_info.get("advertiser"),
        "ad_count_visible": page_info.get("ad_count_visible"),
        "ads_returned": len(cards),
        "ads": cards,
        "transparency_center_url": page_info.get("final_url"),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(duration_s, 1),
        "notes": [
            "Dit zijn publieke gegevens uit Google Ads Transparency Center, geen Google Ads-accountdata.",
            "Google toont previews en aantallen afhankelijk van regio, loginstatus en actuele beschikbaarheid.",
            "De MCP volgt geen advertenties, landingspagina's of trackinglinks en maakt geen wijzigingen.",
        ],
    }


async def run_transparency_search(
    query: str,
    region: str = "anywhere",
    max_ads: int = 10,
) -> Dict[str, Any]:
    query = (query or "").strip()
    if len(query) < 2:
        raise CaptureError("query moet minstens 2 tekens bevatten.")
    if len(query) > 200:
        raise CaptureError("query mag maximaal 200 tekens bevatten.")
    region = normalize_region(region)
    if not isinstance(max_ads, int) or isinstance(max_ads, bool) or not 1 <= max_ads <= MAX_ADS:
        raise CaptureError(f"max_ads moet tussen 1 en {MAX_ADS} liggen.")

    started = time.monotonic()
    query_type = "domain" if _domain_from_query(query) else "advertiser"
    async with browser_slot():
        async with hardened_browser() as (browser, _proxy, _version, _sandboxed):
            context, page = await new_hardened_context(browser, "desktop")
            try:
                await _search_page(page, query, region)
                info = await _page_info(page, query, query_type, region)
                info["cards"] = (info.get("cards") or [])[:max_ads]
                return build_transparency_report(
                    query, region, query_type, info, time.monotonic() - started
                )
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(context.close(), 10)
