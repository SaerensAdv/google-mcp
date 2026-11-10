"""Google Ads Keyword Planner tools (read-only).

Tools:
  keyword_ideas               - keyword ideas from seed keywords and/or a page URL
  keyword_historical_metrics  - search volume, 12-month trend, competition, bids

Both tools only call the KeywordPlanIdeaService "generate" methods, which do
not create or save anything in the Google Ads account.

Call register(mcp) to add the tools to an existing FastMCP instance.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "1010"  # Dutch
DEFAULT_LOCATIONS = ["2056"]  # Belgium

MAX_IDEAS = 200
MAX_HISTORICAL_KEYWORDS = 100
MAX_LOCATIONS = 10
# One API page; ideas are sorted by volume locally, so fetch one large page
# instead of paging (Keyword Planner has strict rate limits).
_IDEAS_PAGE_SIZE = 1000

# Common Dutch language names -> ISO code, so "Nederlands"/"Frans" also work.
_LANGUAGE_ALIASES = {
    "nederlands": "nl",
    "vlaams": "nl",
    "frans": "fr",
    "engels": "en",
    "duits": "de",
    "spaans": "es",
    "italiaans": "it",
}

# Names for the defaults, so the output is readable without an extra lookup.
_KNOWN_LANGUAGES = {"1010": "Dutch"}
_KNOWN_LOCATIONS = {"2056": "Belgium"}

_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11,
    "DECEMBER": 12,
}


class KeywordPlannerInputError(ValueError):
    """Invalid tool input (shown to the caller as-is)."""


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def clean_customer_id(customer_id: str) -> str:
    cid = re.sub(r"[\s-]", "", str(customer_id or ""))
    if not cid.isdigit():
        raise KeywordPlannerInputError(
            "customer_id must be a Google Ads customer ID (digits, hyphens allowed). "
            "Use list_customers to find it."
        )
    return cid


def clean_keywords(keywords: Optional[List[str]], limit: int) -> List[str]:
    seen = set()
    out = []
    for kw in keywords or []:
        kw = " ".join(str(kw).split())
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            out.append(kw)
    if len(out) > limit:
        raise KeywordPlannerInputError(f"Too many keywords: {len(out)} (max {limit}).")
    return out


def resolve_language(client, customer_id: str, language: str) -> Dict[str, Any]:
    """Accepts a language constant ID ("1010"), resource name, ISO code ("nl"),
    English name ("Dutch") or common Dutch name ("Nederlands")."""
    value = str(language or DEFAULT_LANGUAGE).strip()
    m = re.fullmatch(r"(?:languageConstants/)?(\d+)", value)
    if m:
        lid = m.group(1)
        return {"id": lid, "name": _KNOWN_LANGUAGES.get(lid), "resource_name": f"languageConstants/{lid}"}

    wanted = _LANGUAGE_ALIASES.get(value.lower(), value.lower())
    ga = client.get_service("GoogleAdsService")
    rows = ga.search(
        customer_id=customer_id,
        query=(
            "SELECT language_constant.id, language_constant.name, language_constant.code "
            "FROM language_constant WHERE language_constant.targetable = TRUE"
        ),
    )
    for row in rows:
        lc = row.language_constant
        if wanted in (str(lc.code).lower(), str(lc.name).lower()):
            return {
                "id": str(lc.id),
                "name": lc.name,
                "resource_name": f"languageConstants/{lc.id}",
            }
    raise KeywordPlannerInputError(
        f"Unknown language '{value}'. Use an ISO code (e.g. 'nl', 'fr', 'en'), "
        "an English name (e.g. 'Dutch') or a language constant ID (e.g. '1010')."
    )


def _pick_suggestion(term: str, suggestions: list):
    """Prefer an exact name match (highest reach), else the highest reach."""
    term_l = term.strip().lower()
    candidates = [s for s in suggestions if str(s.search_term).lower() == term_l] or suggestions
    exact = [
        s for s in candidates
        if str(s.geo_target_constant.name).lower() == term_l
        or str(s.geo_target_constant.canonical_name).split(",")[0].lower() == term_l
    ]
    pool = exact or candidates
    return max(pool, key=lambda s: int(s.reach or 0)) if pool else None


def resolve_locations(client, locations: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Accepts geo target constant IDs ("2056"), resource names, or place names
    ("België", "Gent", "Belgium")."""
    values = [str(v).strip() for v in (locations or DEFAULT_LOCATIONS) if str(v).strip()]
    if not values:
        values = list(DEFAULT_LOCATIONS)
    if len(values) > MAX_LOCATIONS:
        raise KeywordPlannerInputError(f"Too many locations: {len(values)} (max {MAX_LOCATIONS}).")

    resolved: List[Dict[str, Any]] = []
    names: List[str] = []
    for v in values:
        m = re.fullmatch(r"(?:geoTargetConstants/)?(\d+)", v)
        if m:
            gid = m.group(1)
            resolved.append({"id": gid, "name": _KNOWN_LOCATIONS.get(gid), "resource_name": f"geoTargetConstants/{gid}"})
        else:
            names.append(v)

    if names:
        svc = client.get_service("GeoTargetConstantService")
        req = client.get_type("SuggestGeoTargetConstantsRequest")
        req.locale = "nl"
        req.location_names.names.extend(names)
        suggestions = list(svc.suggest_geo_target_constants(request=req).geo_target_constant_suggestions)
        for name in names:
            best = _pick_suggestion(name, [s for s in suggestions if str(s.search_term).lower() == name.lower()])
            if best is None:
                raise KeywordPlannerInputError(
                    f"Unknown location '{name}'. Use a place name (e.g. 'België', 'Gent') "
                    "or a geo target constant ID (e.g. '2056' for Belgium)."
                )
            g = best.geo_target_constant
            gid = str(g.resource_name).rsplit("/", 1)[-1]
            resolved.append({
                "id": gid,
                "name": g.canonical_name or g.name,
                "type": g.target_type,
                "resource_name": g.resource_name,
            })
    return resolved


def get_currency(client, customer_id: str) -> Optional[str]:
    ga = client.get_service("GoogleAdsService")
    for row in ga.search(customer_id=customer_id, query="SELECT customer.currency_code FROM customer"):
        return row.customer.currency_code or None
    return None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _money(micros) -> Optional[float]:
    micros = int(micros or 0)
    return round(micros / 1_000_000, 2) if micros else None


def _enum_name(value) -> Optional[str]:
    name = getattr(value, "name", None)
    if name is None:
        return str(value) if value is not None else None
    return None if name in ("UNSPECIFIED", "UNKNOWN") else name


def format_metrics(metrics, include_trend: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "avg_monthly_searches": int(getattr(metrics, "avg_monthly_searches", 0) or 0),
        "competition": _enum_name(getattr(metrics, "competition", None)),
        "competition_index": int(getattr(metrics, "competition_index", 0) or 0),
        "low_top_of_page_bid": _money(getattr(metrics, "low_top_of_page_bid_micros", 0)),
        "high_top_of_page_bid": _money(getattr(metrics, "high_top_of_page_bid_micros", 0)),
    }
    if include_trend:
        months = []
        for mv in getattr(metrics, "monthly_search_volumes", []) or []:
            month_num = _MONTHS.get(getattr(mv.month, "name", str(mv.month)))
            if not month_num:
                continue
            months.append((int(mv.year), month_num, int(mv.monthly_searches or 0)))
        months.sort()
        out["monthly_searches"] = [
            {"month": f"{y:04d}-{m:02d}", "searches": s} for y, m, s in months[-12:]
        ]
    return out


def _ads_error(ex) -> str:
    msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    text = f"Request ID: {ex.request_id}\n" + "\n".join(msgs)
    lowered = text.lower()
    if (
        "resource_exhausted" in lowered
        or "too many requests" in lowered
        or ("rate" in lowered and "exceed" in lowered)
    ):
        text += "\nKeyword Planner rate limit reached: wait about a minute and try again."
    return text


# ---------------------------------------------------------------------------
# Core logic (client is injected so it can be tested with fakes)
# ---------------------------------------------------------------------------


def generate_keyword_ideas(
    client,
    customer_id: str,
    keywords: Optional[List[str]] = None,
    page_url: str = "",
    language: str = DEFAULT_LANGUAGE,
    locations: Optional[List[str]] = None,
    limit: int = 50,
    include_adult_keywords: bool = False,
) -> Dict[str, Any]:
    cid = clean_customer_id(customer_id)
    seeds = clean_keywords(keywords, 20)
    url = (page_url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not seeds and not url:
        raise KeywordPlannerInputError("Give at least one seed keyword or a page_url.")
    limit = max(1, min(int(50 if limit is None else limit), MAX_IDEAS))

    lang = resolve_language(client, cid, language)
    locs = resolve_locations(client, locations)
    currency = get_currency(client, cid)

    req = client.get_type("GenerateKeywordIdeasRequest")
    req.customer_id = cid
    req.language = lang["resource_name"]
    req.geo_target_constants.extend([l["resource_name"] for l in locs])
    req.include_adult_keywords = bool(include_adult_keywords)
    req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    req.page_size = _IDEAS_PAGE_SIZE
    if seeds and url:
        req.keyword_and_url_seed.url = url
        req.keyword_and_url_seed.keywords.extend(seeds)
    elif url:
        req.url_seed.url = url
    else:
        req.keyword_seed.keywords.extend(seeds)

    svc = client.get_service("KeywordPlanIdeaService")
    ideas = []
    for i, idea in enumerate(svc.generate_keyword_ideas(request=req)):
        if i >= _IDEAS_PAGE_SIZE:
            break
        row = {"keyword": idea.text}
        row.update(format_metrics(idea.keyword_idea_metrics, include_trend=False))
        ideas.append(row)

    ideas.sort(key=lambda r: (-r["avg_monthly_searches"], r["keyword"]))
    return {
        "customer_id": cid,
        "currency": currency,
        "language": {k: v for k, v in lang.items() if k != "resource_name"},
        "locations": [{k: v for k, v in l.items() if k != "resource_name"} for l in locs],
        "network": "GOOGLE_SEARCH",
        "seed_keywords": seeds,
        "page_url": url or None,
        "total_ideas_found": len(ideas),
        "count": min(limit, len(ideas)),
        "ideas": ideas[:limit],
    }


def generate_historical_metrics(
    client,
    customer_id: str,
    keywords: List[str],
    language: str = DEFAULT_LANGUAGE,
    locations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cid = clean_customer_id(customer_id)
    kws = clean_keywords(keywords, MAX_HISTORICAL_KEYWORDS)
    if not kws:
        raise KeywordPlannerInputError("Give at least one keyword.")

    lang = resolve_language(client, cid, language)
    locs = resolve_locations(client, locations)
    currency = get_currency(client, cid)

    req = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    req.customer_id = cid
    req.keywords.extend(kws)
    req.language = lang["resource_name"]
    req.geo_target_constants.extend([l["resource_name"] for l in locs])
    req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH

    svc = client.get_service("KeywordPlanIdeaService")
    response = svc.generate_keyword_historical_metrics(request=req)

    rows = []
    covered = set()
    for result in response.results:
        row = {"keyword": result.text}
        variants = [v for v in (result.close_variants or []) if v.lower() != str(result.text).lower()]
        if variants:
            row["close_variants"] = list(variants)
        row.update(format_metrics(result.keyword_metrics, include_trend=True))
        if not row["avg_monthly_searches"] and not row["monthly_searches"]:
            continue  # Google returned no data: reported under no_data
        rows.append(row)
        covered.add(str(result.text).lower())
        covered.update(v.lower() for v in variants)

    rows.sort(key=lambda r: (-r["avg_monthly_searches"], r["keyword"]))
    no_data = [k for k in kws if k.lower() not in covered]
    return {
        "customer_id": cid,
        "currency": currency,
        "language": {k: v for k, v in lang.items() if k != "resource_name"},
        "locations": [{k: v for k, v in l.items() if k != "resource_name"} for l in locs],
        "network": "GOOGLE_SEARCH",
        "count": len(rows),
        "keywords": rows,
        "no_data": no_data,
    }


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


def _run(fn, *args, **kwargs) -> str:
    from fastmcp.exceptions import ToolError
    from google.ads.googleads.errors import GoogleAdsException
    import ads_mcp.utils as utils

    try:
        client = utils.get_googleads_client()
        return json.dumps(fn(client, *args, **kwargs), ensure_ascii=False)
    except KeywordPlannerInputError as e:
        raise ToolError(str(e))
    except GoogleAdsException as ex:
        raise ToolError(_ads_error(ex))


def register(mcp) -> None:
    from mcp.types import ToolAnnotations

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def keyword_ideas(
        customer_id: str,
        keywords: List[str] = [],
        page_url: str = "",
        language: str = DEFAULT_LANGUAGE,
        locations: List[str] = DEFAULT_LOCATIONS,
        limit: int = 50,
        include_adult_keywords: bool = False,
    ) -> str:
        """Google Ads Keyword Planner: keyword ideas with search volume, competition and bids.

        Read-only: nothing is saved in the Google Ads account.

        Args:
            customer_id: Google Ads client account ID (use list_customers; hyphens allowed).
            keywords: Up to 20 seed keywords (e.g. ["tandarts gent"]). Optional if page_url is given.
            page_url: Optional page URL to get ideas from (combined with keywords if both given).
            language: Language ID, ISO code or name. Default "1010" (Dutch). E.g. "fr", "English".
            locations: Up to 10 location IDs or place names. Default ["2056"] (Belgium).
                E.g. ["Gent"], ["België", "Nederland"], ["2528"].
            limit: Max ideas returned, sorted by monthly searches (default 50, max 200).
            include_adult_keywords: Include adult keywords (default false).

        Returns JSON with currency, resolved language/locations and ideas:
        keyword, avg_monthly_searches, competition (LOW/MEDIUM/HIGH), competition_index (0-100),
        low_top_of_page_bid and high_top_of_page_bid in the account currency (null = no estimate).
        Network: Google Search only.
        """
        return _run(
            generate_keyword_ideas,
            customer_id,
            keywords=keywords,
            page_url=page_url,
            language=language,
            locations=locations,
            limit=limit,
            include_adult_keywords=include_adult_keywords,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def keyword_historical_metrics(
        customer_id: str,
        keywords: List[str],
        language: str = DEFAULT_LANGUAGE,
        locations: List[str] = DEFAULT_LOCATIONS,
    ) -> str:
        """Google Ads Keyword Planner: historical metrics for specific keywords.

        Read-only: nothing is saved in the Google Ads account.

        Args:
            customer_id: Google Ads client account ID (use list_customers; hyphens allowed).
            keywords: Up to 100 keywords to look up.
            language: Language ID, ISO code or name. Default "1010" (Dutch).
            locations: Up to 10 location IDs or place names. Default ["2056"] (Belgium).

        Returns JSON per keyword (sorted by volume): avg_monthly_searches, monthly_searches
        for the last 12 months, competition, competition_index, low/high top-of-page bid in the
        account currency, and close_variants Google grouped with it. Keywords without data are
        listed under no_data. Network: Google Search only.
        """
        return _run(
            generate_historical_metrics,
            customer_id,
            keywords=keywords,
            language=language,
            locations=locations,
        )
