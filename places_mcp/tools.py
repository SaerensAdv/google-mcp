"""Google Places (New) tools for local competitor research (read-only).

Tools (prefix places_):
  places_search         - text search ("tandarts Gent"), optionally around a location
  places_details        - details + latest reviews (max 5) for one place

Auth: existing GOOGLE_API_KEY (sent as header, never in URLs or logs).
Cost: Google bills every Places request. Only needed fields are requested
(field masks) and a server-side daily cap (PLACES_DAILY_CAP, default 200)
limits the number of calls.

Call register(mcp) to add the tools to an existing FastMCP instance.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

import httpx

from places_mcp.usage import DailyCapReached, reserve_call

logger = logging.getLogger(__name__)

BASE = "https://places.googleapis.com/v1"
TIMEOUT_SECONDS = 30
MAX_RESULTS = 20
MAX_RADIUS_KM = 50.0
DEFAULT_LANGUAGE = "nl"
DEFAULT_REGION = "BE"

SEARCH_FIELDS = ",".join(
    "places." + f
    for f in (
        "id", "displayName", "formattedAddress", "location", "primaryTypeDisplayName",
        "googleMapsUri", "businessStatus", "rating", "userRatingCount", "websiteUri",
    )
)
GEOCODE_FIELDS = "places.location,places.formattedAddress"
DETAILS_FIELDS = ",".join((
    "id", "displayName", "formattedAddress", "location", "primaryTypeDisplayName", "types",
    "googleMapsUri", "businessStatus", "rating", "userRatingCount", "websiteUri",
    "nationalPhoneNumber", "regularOpeningHours.weekdayDescriptions",
))
REVIEW_FIELDS = "reviews"


class PlacesError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise PlacesError("Missing GOOGLE_API_KEY in Replit Secrets.")
    return key


def translate_error(status: int, payload: Any) -> PlacesError:
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = str(err.get("message") or payload or "")[:400]
    reasons = {
        d.get("reason") for d in err.get("details", []) or [] if isinstance(d, dict) and d.get("reason")
    }
    if "SERVICE_DISABLED" in reasons or "has not been used in project" in message:
        return PlacesError(
            "Places API (New) is not enabled in the Google Cloud project of GOOGLE_API_KEY. "
            "Enable 'Places API (New)' in Google Cloud Console (APIs & Services > Library), "
            "wait a few minutes and retry."
        )
    if "API_KEY_SERVICE_BLOCKED" in reasons or "are blocked" in message:
        return PlacesError(
            "GOOGLE_API_KEY is restricted and does not allow Places API (New). Add 'Places API (New)' "
            "to the key's API restrictions in Google Cloud Console (APIs & Services > Credentials)."
        )
    if "BILLING_DISABLED" in reasons or "billing" in message.lower():
        return PlacesError("Places API needs an active billing account on the Google Cloud project.")
    if "API_KEY_INVALID" in reasons:
        return PlacesError("GOOGLE_API_KEY is invalid.")
    if status == 429:
        return PlacesError("Places API rate limit/quota reached at Google. Try again later.")
    if status == 404:
        return PlacesError(f"Place not found. {message}")
    return PlacesError(f"Places API error ({status}): {message}")


def _request(method: str, path: str, field_mask: str, body: Optional[Dict] = None,
             params: Optional[Dict] = None, label: str = "") -> Dict[str, Any]:
    key = _api_key()
    used, cap = reserve_call()
    logger.info("Places call %s (%d/%d today)", label or path.split("/")[-1], used, cap)
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": field_mask}
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        resp = client.request(method, f"{BASE}/{path}", headers=headers, json=body, params=params)
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text[:400]
    if resp.status_code >= 400:
        err = translate_error(resp.status_code, payload)
        logger.warning("Places %s failed: %s", label or path, err)
        raise err
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _distance_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _latlng(p: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    loc = p.get("location") or {}
    if "latitude" in loc and "longitude" in loc:
        return float(loc["latitude"]), float(loc["longitude"])
    return None


def resolve_center(near: str, language: str, region: str) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
    """'51.05,3.72' -> coordinates (no API call); a place name -> one Places lookup."""
    near = (near or "").strip()
    if not near:
        return None, None
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", near)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise PlacesError("near: invalid coordinates.")
        return (lat, lng), f"{lat},{lng}"
    data = _request("POST", "places:searchText", GEOCODE_FIELDS, label="locate",
                    body={"textQuery": near, "pageSize": 1, "languageCode": language, "regionCode": region})
    places = data.get("places") or []
    center = _latlng(places[0]) if places else None
    if not center:
        raise PlacesError(f"Location '{near}' not found. Use a city/address or 'lat,lng'.")
    return center, places[0].get("formattedAddress") or near


def format_place(p: Dict[str, Any], center: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
    row = {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text"),
        "category": (p.get("primaryTypeDisplayName") or {}).get("text"),
        "rating": p.get("rating"),
        "reviews": int(p.get("userRatingCount") or 0),
        "address": p.get("formattedAddress"),
        "website": p.get("websiteUri"),
        "maps_url": p.get("googleMapsUri"),
    }
    status = p.get("businessStatus")
    if status and status != "OPERATIONAL":
        row["business_status"] = status
    pos = _latlng(p)
    if center and pos:
        row["distance_km"] = round(_distance_km(center, pos), 1)
    return row


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def search_places(query: str, near: str = "", radius_km: float = 0, strict_radius: bool = False,
                  limit: int = MAX_RESULTS, language: str = DEFAULT_LANGUAGE,
                  region: str = DEFAULT_REGION) -> Dict[str, Any]:
    query = " ".join(str(query or "").split())
    if not query:
        raise PlacesError("query is required, e.g. 'tandarts Gent'.")
    limit = max(1, min(int(limit or MAX_RESULTS), MAX_RESULTS))
    radius_km = float(radius_km or 0)
    if radius_km < 0 or radius_km > MAX_RADIUS_KM:
        raise PlacesError(f"radius_km must be between 0 and {MAX_RADIUS_KM:g}.")
    language = (language or DEFAULT_LANGUAGE).strip()
    region = (region or DEFAULT_REGION).strip().upper()

    center, center_label = resolve_center(near, language, region)
    if radius_km and not center:
        raise PlacesError("radius_km needs 'near' (a place name or 'lat,lng').")

    body: Dict[str, Any] = {"textQuery": query, "pageSize": MAX_RESULTS, "languageCode": language,
                            "regionCode": region}
    if center:
        body["locationBias"] = {"circle": {
            "center": {"latitude": center[0], "longitude": center[1]},
            "radius": min(radius_km or 10.0, MAX_RADIUS_KM) * 1000,
        }}
    data = _request("POST", "places:searchText", SEARCH_FIELDS, body=body, label="search")
    rows = [format_place(p, center) for p in data.get("places") or []]

    dropped = 0
    if strict_radius and center and radius_km:
        kept = [r for r in rows if r.get("distance_km") is not None and r["distance_km"] <= radius_km]
        dropped = len(rows) - len(kept)
        rows = kept
    rows = rows[:limit]
    for i, r in enumerate(rows, 1):
        r["position"] = i

    rated = [r["rating"] for r in rows if r["rating"] is not None]
    counts = [r["reviews"] for r in rows]
    return {
        "query": query,
        "near": center_label,
        "radius_km": radius_km or None,
        "strict_radius": bool(strict_radius and center and radius_km),
        "count": len(rows),
        "outside_radius_removed": dropped,
        "summary": {
            "avg_rating": round(statistics.mean(rated), 2) if rated else None,
            "median_reviews": statistics.median(counts) if counts else None,
            "with_website": sum(1 for r in rows if r["website"]),
        },
        "results": rows,
        "note": "Order = Google's relevance order for this search (not a Maps ranking).",
    }


def place_details(place_id: str, include_reviews: bool = True, language: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    pid = re.sub(r"^places/", "", str(place_id or "").strip())
    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,}", pid):
        raise PlacesError("place_id must be a Google place ID (from places_search).")
    mask = DETAILS_FIELDS + ("," + REVIEW_FIELDS if include_reviews else "")
    p = _request("GET", f"places/{pid}", mask, params={"languageCode": language or DEFAULT_LANGUAGE},
                 label="details")
    row = format_place(p)
    row["types"] = p.get("types")
    row["phone"] = p.get("nationalPhoneNumber")
    row["opening_hours"] = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    if include_reviews:
        row["latest_reviews"] = [
            {
                "rating": r.get("rating"),
                "when": r.get("relativePublishTimeDescription"),
                "published": r.get("publishTime"),
                "author": (r.get("authorAttribution") or {}).get("displayName"),
                "text": ((r.get("text") or r.get("originalText") or {}).get("text") or "")[:1500],
            }
            for r in (p.get("reviews") or [])[:5]
        ]
        row["reviews_note"] = (
            "Google returns at most 5 reviews (its selection, not all reviews)."
            if row["latest_reviews"] else
            "Google returned no review texts for this place via the API (rating and review count "
            "are still correct). Read the reviews via maps_url."
        )
    return row


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


def _run(fn, *args, **kwargs) -> str:
    from fastmcp.exceptions import ToolError

    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except (PlacesError, DailyCapReached) as e:
        raise ToolError(str(e))
    except httpx.HTTPError as e:
        raise ToolError(f"Places API network error: {type(e).__name__}")


def register(mcp) -> None:
    from mcp.types import ToolAnnotations

    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

    @mcp.tool(annotations=ro)
    def places_search(
        query: str,
        near: str = "",
        radius_km: float = 0,
        strict_radius: bool = False,
        limit: int = 20,
        language: str = DEFAULT_LANGUAGE,
        region: str = DEFAULT_REGION,
    ) -> str:
        """Find local businesses/competitors on Google Maps (Places API, read-only).

        Each Places call costs money and counts toward a daily cap.

        Args:
            query: What to search, ideally type + place, e.g. "tandarts Gent", "kapper Brugge".
            near: Optional center as place name ("Gent", "Kouter 1, Gent") or "lat,lng".
                A place name costs one extra Places call.
            radius_km: Optional radius around `near` (max 50). Default bias radius 10 km.
            strict_radius: Drop results farther than radius_km from `near` (default false).
            limit: Max results (default and max 20).
            language: Result language (default "nl").
            region: Region code for interpretation (default "BE").

        Returns per result: position, name, category, rating, reviews (count), address,
        website, maps_url, place_id, distance_km (when `near` is given), plus a summary
        (avg rating, median reviews). Use places_details for reviews of one business.
        """
        return _run(search_places, query, near, radius_km, strict_radius, limit, language, region)

    @mcp.tool(annotations=ro)
    def places_details(place_id: str, include_reviews: bool = True, language: str = DEFAULT_LANGUAGE) -> str:
        """Details of one Google Maps business, including up to 5 of the latest reviews Google provides.

        Args:
            place_id: place_id from places_search.
            include_reviews: Include reviews (default true; slightly more expensive at Google).
            language: Language for texts (default "nl").

        Returns name, category, types, rating, review count, address, phone, website,
        opening hours, maps_url and latest_reviews (rating, when, author, text).
        """
        return _run(place_details, place_id, include_reviews, language)
