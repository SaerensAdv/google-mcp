"""Google Business Profile MCP tools (read-only).

All tools are prefixed with gbp_. Call register(mcp) to add them to an existing
FastMCP instance. Only GET requests are made (see gbp_mcp/auth.py): nothing in
a Business Profile can be changed through these tools.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ads_mcp.google_rest import GoogleApiError
from gbp_mcp import auth

logger = logging.getLogger(__name__)

ACCOUNT_PAGE_SIZE = 20       # Account Management API max
LOCATION_PAGE_SIZE = 100     # Business Information API max
REVIEW_PAGE_SIZE = 50        # My Business v4 max
KEYWORD_PAGE_SIZE = 100      # Performance API max
MAX_REVIEWS = 200
MAX_KEYWORDS = 500
MAX_PERFORMANCE_DAYS = 550   # Google keeps ~18 months
COMMENT_MAX_CHARS = 1500

LIST_READ_MASK = (
    "name,title,storeCode,storefrontAddress,phoneNumbers,websiteUri,categories.primaryCategory,"
    "openInfo.status,metadata.placeId,metadata.mapsUri"
)
DETAIL_READ_MASK = (
    "name,title,storeCode,languageCode,phoneNumbers,categories,storefrontAddress,websiteUri,"
    "regularHours,specialHours,serviceArea,labels,latlng,openInfo,metadata,profile"
)

# Performance API daily metric -> output key
DAILY_METRICS = {
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": "views_search_desktop",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": "views_search_mobile",
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": "views_maps_desktop",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS": "views_maps_mobile",
    "CALL_CLICKS": "calls",
    "WEBSITE_CLICKS": "website_clicks",
    "BUSINESS_DIRECTION_REQUESTS": "direction_requests",
    "BUSINESS_CONVERSATIONS": "conversations",
    "BUSINESS_BOOKINGS": "bookings",
    "BUSINESS_FOOD_ORDERS": "food_orders",
    "BUSINESS_FOOD_MENU_CLICKS": "food_menu_clicks",
}
STAR_RATINGS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
REVIEW_ORDER = {"newest": "updateTime desc", "lowest": "rating", "highest": "rating desc"}


class GbpInputError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def clean_account_id(account_id: str) -> str:
    aid = re.sub(r"^accounts/", "", str(account_id or "").strip())
    if not aid.isdigit():
        raise GbpInputError("account_id must be a numeric Business Profile account ID. Use gbp_list_accounts.")
    return aid


def clean_location_id(location_id: str) -> Tuple[str, Optional[str]]:
    """Return (location_id, account_id or None). Accepts '123', 'locations/123'
    or 'accounts/9/locations/123'."""
    raw = str(location_id or "").strip()
    m = re.fullmatch(r"(?:accounts/(\d+)/)?(?:locations/)?(\d+)", raw)
    if not m:
        raise GbpInputError("location_id must be a numeric Business Profile location ID. Use gbp_list_locations.")
    return m.group(2), m.group(1)


def _parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise GbpInputError(f"{field} must be a date in YYYY-MM-DD format.") from None


def _parse_month(value: str, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m").date()
    except ValueError:
        raise GbpInputError(f"{field} must be a month in YYYY-MM format.") from None


def _prev_month(d: date, n: int = 1) -> date:
    y, m = d.year, d.month - n
    while m < 1:
        m += 12
        y -= 1
    return date(y, m, 1)


def _today() -> date:
    return date.today()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _address(addr: Optional[Dict[str, Any]]) -> Optional[str]:
    if not addr:
        return None
    parts = list(addr.get("addressLines") or [])
    city = " ".join(p for p in (addr.get("postalCode"), addr.get("locality")) if p)
    if city:
        parts.append(city)
    if addr.get("regionCode"):
        parts.append(addr["regionCode"])
    return ", ".join(parts) or None


def _time(t: Optional[Dict[str, Any]]) -> str:
    t = t or {}
    return f"{int(t.get('hours', 0) or 0):02d}:{int(t.get('minutes', 0) or 0):02d}"


def _gdate(d: Optional[Dict[str, Any]]) -> Optional[str]:
    if not d or not d.get("year"):
        return None
    return f"{int(d['year']):04d}-{int(d.get('month') or 1):02d}-{int(d.get('day') or 1):02d}"


def _location_id(name: str) -> str:
    return str(name or "").rsplit("/", 1)[-1]


def format_location_summary(loc: Dict[str, Any]) -> Dict[str, Any]:
    meta = loc.get("metadata") or {}
    phones = loc.get("phoneNumbers") or {}
    primary = (loc.get("categories") or {}).get("primaryCategory") or {}
    return {
        "location_id": _location_id(loc.get("name")),
        "title": loc.get("title"),
        "store_code": loc.get("storeCode"),
        "address": _address(loc.get("storefrontAddress")),
        "phone": phones.get("primaryPhone"),
        "website": loc.get("websiteUri"),
        "primary_category": primary.get("displayName"),
        "open_status": (loc.get("openInfo") or {}).get("status"),
        "place_id": meta.get("placeId"),
        "maps_url": meta.get("mapsUri"),
    }


def format_location_details(loc: Dict[str, Any]) -> Dict[str, Any]:
    out = format_location_summary(loc)
    cats = loc.get("categories") or {}
    phones = loc.get("phoneNumbers") or {}
    meta = loc.get("metadata") or {}
    open_info = loc.get("openInfo") or {}

    hours = [
        {
            "open_day": p.get("openDay"),
            "open": _time(p.get("openTime")),
            "close_day": p.get("closeDay"),
            "close": _time(p.get("closeTime")),
        }
        for p in (loc.get("regularHours") or {}).get("periods", []) or []
    ]
    special = []
    for p in (loc.get("specialHours") or {}).get("specialHourPeriods", []) or []:
        entry = {"date": _gdate(p.get("startDate")), "closed": bool(p.get("closed", False))}
        if p.get("endDate"):
            entry["end_date"] = _gdate(p.get("endDate"))
        if not entry["closed"]:
            entry["open"] = _time(p.get("openTime"))
            entry["close"] = _time(p.get("closeTime"))
        special.append(entry)
    area = loc.get("serviceArea") or {}
    places = [
        pi.get("placeName")
        for pi in ((area.get("places") or {}).get("placeInfos") or [])
        if pi.get("placeName")
    ]
    latlng = loc.get("latlng") or {}

    out.update({
        "language": loc.get("languageCode"),
        "additional_phones": phones.get("additionalPhones") or [],
        "additional_categories": [
            c.get("displayName") for c in cats.get("additionalCategories") or [] if c.get("displayName")
        ],
        "description": (loc.get("profile") or {}).get("description"),
        "opening_date": _gdate(open_info.get("openingDate")),
        "regular_hours": hours,
        "special_hours": special,
        "service_area": {
            "business_type": area.get("businessType"),
            "places": places,
        } if area else None,
        "labels": loc.get("labels") or [],
        "latlng": {"lat": latlng.get("latitude"), "lng": latlng.get("longitude")} if latlng else None,
        "new_review_url": meta.get("newReviewUri"),
        "has_voice_of_merchant": meta.get("hasVoiceOfMerchant"),
        "has_pending_edits": meta.get("hasPendingEdits"),
        "duplicate_of": meta.get("duplicateLocation"),
    })
    return out


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def list_accounts() -> Dict[str, Any]:
    accounts, token, pages = [], None, 0
    while pages < 10:
        data = auth.get_json("accounts", "accounts", {"pageSize": ACCOUNT_PAGE_SIZE, "pageToken": token})
        accounts.extend(data.get("accounts", []))
        token = data.get("nextPageToken")
        pages += 1
        if not token:
            break
    rows = [
        {
            "account_id": _location_id(a.get("name")),
            "name": a.get("accountName"),
            "type": a.get("type"),
            "role": a.get("role"),
            "verification_state": a.get("verificationState"),
        }
        for a in accounts
    ]
    rows.sort(key=lambda r: ((r["type"] != "PERSONAL"), (r["name"] or "").lower()))
    return {
        "count": len(rows),
        "accounts": rows,
        "hint": "Pass account_id to gbp_list_locations. Client profiles are often in a "
                "LOCATION_GROUP (business group) account rather than the PERSONAL one.",
    }


def list_locations(account_id: str, limit: int = 100, page_token: str = "") -> Dict[str, Any]:
    aid = clean_account_id(account_id)
    limit = max(1, min(int(100 if limit is None else limit), LOCATION_PAGE_SIZE))
    data = auth.get_json(
        "info", f"accounts/{aid}/locations",
        {"readMask": LIST_READ_MASK, "pageSize": limit, "pageToken": page_token},
    )
    rows = [format_location_summary(loc) for loc in data.get("locations", [])]
    return {
        "account_id": aid,
        "count": len(rows),
        "total": data.get("totalSize"),
        "next_page_token": data.get("nextPageToken"),
        "locations": rows,
    }


def location_details(location_id: str) -> Dict[str, Any]:
    lid, _ = clean_location_id(location_id)
    data = auth.get_json("info", f"locations/{lid}", {"readMask": DETAIL_READ_MASK})
    return format_location_details(data)


def performance(
    location_id: str,
    start_date: str = "",
    end_date: str = "",
    include_daily: bool = False,
) -> Dict[str, Any]:
    lid, _ = clean_location_id(location_id)
    end = _parse_date(end_date, "end_date") if end_date else _today() - timedelta(days=1)
    start = _parse_date(start_date, "start_date") if start_date else end - timedelta(days=27)
    if start > end:
        raise GbpInputError("start_date must be on or before end_date.")
    if (end - start).days + 1 > MAX_PERFORMANCE_DAYS:
        raise GbpInputError(f"Date range too long (max {MAX_PERFORMANCE_DAYS} days).")

    params = {
        "dailyMetrics": list(DAILY_METRICS),
        "dailyRange.startDate.year": start.year,
        "dailyRange.startDate.month": start.month,
        "dailyRange.startDate.day": start.day,
        "dailyRange.endDate.year": end.year,
        "dailyRange.endDate.month": end.month,
        "dailyRange.endDate.day": end.day,
    }
    data = auth.get_json("performance", f"locations/{lid}:fetchMultiDailyMetricsTimeSeries", params)

    totals = {key: 0 for key in DAILY_METRICS.values()}
    daily: Dict[str, Dict[str, int]] = {}
    for multi in data.get("multiDailyMetricTimeSeries", []) or []:
        for series in multi.get("dailyMetricTimeSeries", []) or []:
            key = DAILY_METRICS.get(series.get("dailyMetric"))
            if not key:
                continue
            for dv in (series.get("timeSeries") or {}).get("datedValues", []) or []:
                value = int(dv.get("value", 0) or 0)  # Google omits value when 0
                totals[key] += value
                day = _gdate(dv.get("date"))
                if include_daily and day:
                    daily.setdefault(day, {k: 0 for k in DAILY_METRICS.values()})[key] += value

    search = totals["views_search_desktop"] + totals["views_search_mobile"]
    maps = totals["views_maps_desktop"] + totals["views_maps_mobile"]
    out = {
        "location_id": lid,
        "date_range": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1},
        "summary": {
            "profile_views": search + maps,
            "views_google_search": search,
            "views_google_maps": maps,
            "views_mobile": totals["views_search_mobile"] + totals["views_maps_mobile"],
            "views_desktop": totals["views_search_desktop"] + totals["views_maps_desktop"],
            "calls": totals["calls"],
            "website_clicks": totals["website_clicks"],
            "direction_requests": totals["direction_requests"],
            "conversations": totals["conversations"],
            "bookings": totals["bookings"],
            "food_orders": totals["food_orders"],
            "food_menu_clicks": totals["food_menu_clicks"],
        },
        "totals_by_metric": totals,
        "note": "Profile views count unique users per day. Google data lags a few days, "
                "so the most recent days can still be incomplete.",
    }
    if include_daily:
        out["daily"] = [{"date": d, **daily[d]} for d in sorted(daily)]
    return out


def search_keywords(
    location_id: str,
    start_month: str = "",
    end_month: str = "",
    limit: int = 50,
    page_token: str = "",
) -> Dict[str, Any]:
    lid, _ = clean_location_id(location_id)
    last_full = _prev_month(_today().replace(day=1))
    end = _parse_month(end_month, "end_month") if end_month else last_full
    start = _parse_month(start_month, "start_month") if start_month else _prev_month(end, 2)
    if start > end:
        raise GbpInputError("start_month must be on or before end_month.")
    limit = max(1, min(int(50 if limit is None else limit), MAX_KEYWORDS))

    rows: List[Dict[str, Any]] = []
    token = page_token or None
    while len(rows) < limit:
        data = auth.get_json(
            "performance", f"locations/{lid}/searchkeywords/impressions/monthly",
            {
                "monthlyRange.startMonth.year": start.year,
                "monthlyRange.startMonth.month": start.month,
                "monthlyRange.endMonth.year": end.year,
                "monthlyRange.endMonth.month": end.month,
                "pageSize": min(KEYWORD_PAGE_SIZE, limit - len(rows)),
                "pageToken": token,
            },
        )
        for kc in data.get("searchKeywordsCounts", []) or []:
            iv = kc.get("insightsValue") or {}
            if "value" in iv:
                rows.append({"keyword": kc.get("searchKeyword"), "impressions": int(iv["value"]),
                             "below_threshold": False})
            else:
                rows.append({"keyword": kc.get("searchKeyword"),
                             "impressions": int(iv.get("threshold", 0) or 0), "below_threshold": True})
        token = data.get("nextPageToken")
        if not token:
            break
    rows = rows[:limit]
    rows.sort(key=lambda r: (r["below_threshold"], -r["impressions"]))
    return {
        "location_id": lid,
        "month_range": {"start": start.strftime("%Y-%m"), "end": end.strftime("%Y-%m")},
        "count": len(rows),
        "next_page_token": token,
        "keywords": rows,
        "note": "Search terms that showed this profile in Google Search/Maps. When below_threshold is "
                "true Google hides the exact count: impressions is then an upper bound (fewer than that).",
    }


def _format_review(r: Dict[str, Any]) -> Dict[str, Any]:
    reviewer = r.get("reviewer") or {}
    reply = r.get("reviewReply") or None
    comment = r.get("comment")
    if comment and len(comment) > COMMENT_MAX_CHARS:
        comment = comment[:COMMENT_MAX_CHARS] + "…"
    return {
        "review_id": r.get("reviewId"),
        "rating": STAR_RATINGS.get(r.get("starRating")),
        "comment": comment,
        "reviewer": "Anonymous" if reviewer.get("isAnonymous") else reviewer.get("displayName"),
        "created": r.get("createTime"),
        "updated": r.get("updateTime"),
        "reply": {"comment": reply.get("comment"), "updated": reply.get("updateTime")} if reply else None,
    }


def list_reviews(
    location_id: str,
    account_id: str = "",
    limit: int = 50,
    order: str = "newest",
    page_token: str = "",
) -> Dict[str, Any]:
    lid, embedded_aid = clean_location_id(location_id)
    if not account_id and not embedded_aid:
        raise GbpInputError("account_id is required for reviews (the account the location belongs to). "
                            "Use gbp_list_accounts.")
    aid = clean_account_id(account_id or embedded_aid)
    order_key = str(order or "newest").strip().lower()
    if order_key not in REVIEW_ORDER:
        raise GbpInputError("order must be 'newest', 'lowest' or 'highest'.")
    limit = max(1, min(int(50 if limit is None else limit), MAX_REVIEWS))

    reviews: List[Dict[str, Any]] = []
    token = page_token or None
    average = total = None
    while len(reviews) < limit:
        data = auth.get_json(
            "reviews", f"accounts/{aid}/locations/{lid}/reviews",
            {"pageSize": min(REVIEW_PAGE_SIZE, limit - len(reviews)), "pageToken": token,
             "orderBy": REVIEW_ORDER[order_key]},
        )
        if average is None:
            average, total = data.get("averageRating"), data.get("totalReviewCount")
        reviews.extend(data.get("reviews", []) or [])
        token = data.get("nextPageToken")
        if not token:
            break
    rows = [_format_review(r) for r in reviews[:limit]]
    dist = Counter(r["rating"] for r in rows if r["rating"])
    return {
        "account_id": aid,
        "location_id": lid,
        "rating_summary": {
            "average_rating": round(float(average), 2) if average is not None else None,
            "total_reviews": int(total or 0),
            "returned": len(rows),
            "distribution_of_returned": {str(s): dist.get(s, 0) for s in range(5, 0, -1)},
            "returned_without_reply": sum(1 for r in rows if not r["reply"]),
        },
        "order": order_key,
        "next_page_token": token,
        "reviews": rows,
    }


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


def _run(fn, *args, **kwargs) -> str:
    from fastmcp.exceptions import ToolError

    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except (GbpInputError, GoogleApiError) as e:
        raise ToolError(str(e))


def register(mcp) -> None:
    from mcp.types import ToolAnnotations

    ro = ToolAnnotations(readOnlyHint=True)

    @mcp.tool(annotations=ro)
    def gbp_list_accounts() -> str:
        """List the Google Business Profile accounts this login can access (read-only).

        Call this first. Returns account_id, name, type (PERSONAL, LOCATION_GROUP = business
        group, ORGANIZATION) and the login's role. Use account_id with gbp_list_locations.
        """
        return _run(list_accounts)

    @mcp.tool(annotations=ro)
    def gbp_list_locations(account_id: str, limit: int = 100, page_token: str = "") -> str:
        """List the business locations (Business Profiles) in an account.

        Args:
            account_id: Business Profile account ID (from gbp_list_accounts).
            limit: Max locations per call (default 100, max 100).
            page_token: next_page_token from the previous call for the next page.

        Each location: location_id, title, address, phone, website, primary category,
        open status, Google place_id and Maps link.
        """
        return _run(list_locations, account_id, limit, page_token)

    @mcp.tool(annotations=ro)
    def gbp_location_details(location_id: str) -> str:
        """Full details of one Business Profile location.

        Args:
            location_id: Location ID (from gbp_list_locations; "locations/123" also accepted).

        Returns name, address, phones, website, all categories, description, opening hours,
        special hours, service area, labels, coordinates, review link and profile status
        flags (verified/voice of merchant, pending edits, duplicate).
        """
        return _run(location_details, location_id)

    @mcp.tool(annotations=ro)
    def gbp_performance(
        location_id: str, start_date: str = "", end_date: str = "", include_daily: bool = False
    ) -> str:
        """Business Profile performance for a period: profile views (Google Search vs Maps,
        mobile vs desktop), calls, website clicks, direction requests, chats, bookings.

        Args:
            location_id: Location ID (from gbp_list_locations).
            start_date: YYYY-MM-DD (default: 28 days before end_date).
            end_date: YYYY-MM-DD (default: yesterday). Google data lags a few days.
            include_daily: Also return one row per day (default false).
        """
        return _run(performance, location_id, start_date, end_date, include_daily)

    @mcp.tool(annotations=ro)
    def gbp_search_keywords(
        location_id: str, start_month: str = "", end_month: str = "", limit: int = 50, page_token: str = ""
    ) -> str:
        """Search terms people used on Google Search/Maps that showed this Business Profile,
        with impressions over a month range.

        Args:
            location_id: Location ID (from gbp_list_locations).
            start_month: YYYY-MM (default: 2 months before end_month, i.e. 3 months total).
            end_month: YYYY-MM (default: last complete month).
            limit: Max keywords (default 50, max 500).
            page_token: next_page_token from the previous call for more keywords.

        Low-volume terms only have an upper bound (below_threshold = true).
        """
        return _run(search_keywords, location_id, start_month, end_month, limit, page_token)

    @mcp.tool(annotations=ro)
    def gbp_list_reviews(
        location_id: str, account_id: str = "", limit: int = 50, order: str = "newest", page_token: str = ""
    ) -> str:
        """Google reviews of a Business Profile location, with a rating summary.

        Args:
            location_id: Location ID (from gbp_list_locations).
            account_id: Account ID the location belongs to (from gbp_list_accounts). Required.
            limit: Max reviews (default 50, max 200).
            order: "newest" (default), "lowest" (worst first) or "highest".
            page_token: next_page_token from the previous call for more reviews.

        Returns average rating and total review count for the location, the star distribution
        and number without an owner reply among the returned reviews, and per review: stars,
        text, reviewer name, dates and the owner reply (if any). Read-only: cannot reply.
        """
        return _run(list_reviews, location_id, account_id, limit, order, page_token)
