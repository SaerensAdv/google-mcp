"""Google Merchant Center tools (Merchant API v1, read-only).

All tools are prefixed with merchant_. Call register(mcp) to add them to an
existing FastMCP instance. Only GET requests are made.

Auth: shared Google login (ads_mcp/user_credentials.py) with the `content` scope.
Google has no read-only Merchant scope; read-only is enforced by this module
only calling GET endpoints.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Dict, Optional

from ads_mcp import google_rest

logger = logging.getLogger(__name__)

API_LABEL = "Merchant API"
BASE = "https://merchantapi.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/content"]

MAX_PRODUCTS = 250
PRODUCT_SCAN_CAP = 5000  # max products scanned when filtering on status
PAGE_SIZE = 1000


class MerchantInputError(ValueError):
    pass


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return google_rest.get_json(API_LABEL, f"{BASE}/{path}", SCOPES, params)


def clean_account_id(account_id: str) -> str:
    aid = re.sub(r"^accounts/", "", str(account_id or "").strip())
    if not aid.isdigit():
        raise MerchantInputError(
            "account_id must be a numeric Merchant Center account ID. Use merchant_list_accounts."
        )
    return aid


def _short_context(ctx: str) -> str:
    return (ctx or "").replace("_", " ").lower() if ctx else ""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def list_accounts(parent_account_id: str = "") -> Dict[str, Any]:
    if parent_account_id:
        pid = clean_account_id(parent_account_id)
        path, key = f"accounts/v1/accounts/{pid}:listSubaccounts", "accounts"
    else:
        path, key = "accounts/v1/accounts", "accounts"
    accounts, token, pages = [], None, 0
    while pages < 20:
        data = _get(path, {"pageSize": 250, "pageToken": token})
        accounts.extend(data.get(key, []))
        token = data.get("nextPageToken")
        pages += 1
        if not token:
            break
    rows = [
        {
            "account_id": a.get("accountId") or str(a.get("name", "")).rsplit("/", 1)[-1],
            "name": a.get("accountName"),
            "test_account": bool(a.get("testAccount", False)),
            "language": a.get("languageCode"),
            "time_zone": (a.get("timeZone") or {}).get("id"),
        }
        for a in accounts
    ]
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return {
        "parent_account_id": parent_account_id or None,
        "count": len(rows),
        "accounts": rows,
        "hint": "Advanced (multi-client) accounts: pass parent_account_id to list their sub-accounts.",
    }


def account_issues(account_id: str, language: str = "nl") -> Dict[str, Any]:
    aid = clean_account_id(account_id)
    issues, token, pages = [], None, 0
    while pages < 10:
        data = _get(
            f"accounts/v1/accounts/{aid}/issues",
            {"pageSize": 100, "pageToken": token, "languageCode": language or "en", "timeZone": "Europe/Brussels"},
        )
        issues.extend(data.get("accountIssues", []))
        token = data.get("nextPageToken")
        pages += 1
        if not token:
            break
    order = {"CRITICAL": 0, "ERROR": 1, "SUGGESTION": 2}
    rows = []
    for i in issues:
        impacts = []
        for dest in i.get("impactedDestinations", []) or []:
            regions = sorted({imp.get("regionCode") for imp in dest.get("impacts", []) or [] if imp.get("regionCode")})
            impacts.append({"reporting_context": dest.get("reportingContext"), "regions": regions})
        rows.append({
            "title": i.get("title"),
            "severity": i.get("severity"),
            "detail": i.get("detail"),
            "documentation": i.get("documentationUri"),
            "impacted": impacts,
        })
    rows.sort(key=lambda r: order.get(r["severity"], 9))
    counts = Counter(r["severity"] for r in rows)
    return {"account_id": aid, "count": len(rows), "by_severity": dict(counts), "issues": rows}


def product_status_summary(account_id: str, country: str = "", reporting_context: str = "") -> Dict[str, Any]:
    aid = clean_account_id(account_id)
    filters = []
    if country:
        filters.append(f'country = "{country.strip().upper()}"')
    if reporting_context:
        filters.append(f'reporting_context = "{reporting_context.strip().upper()}"')
    statuses, token, pages = [], None, 0
    while pages < 20:
        data = _get(
            f"issueresolution/v1/accounts/{aid}/aggregateProductStatuses",
            {"pageSize": 100, "pageToken": token, "filter": " AND ".join(filters)},
        )
        statuses.extend(data.get("aggregateProductStatuses", []))
        token = data.get("nextPageToken")
        pages += 1
        if not token:
            break

    sev_order = {"DISAPPROVED": 0, "DEMOTED": 1, "NOT_IMPACTED": 2}
    groups = []
    for s in statuses:
        stats = s.get("stats", {}) or {}
        issues = [
            {
                "severity": it.get("severity"),
                "products": int(it.get("productCount", 0) or 0),
                "description": it.get("description"),
                "attribute": it.get("attribute"),
                "resolution": it.get("resolution"),
                "code": it.get("code"),
                "documentation": it.get("documentationUri"),
            }
            for it in s.get("itemLevelIssues", []) or []
        ]
        issues.sort(key=lambda r: (sev_order.get(r["severity"], 9), -r["products"]))
        groups.append({
            # Google also returns groups without a reporting context (newer/unnamed destinations).
            "reporting_context": s.get("reportingContext") or "UNSPECIFIED",
            "country": s.get("country"),
            "active": int(stats.get("activeCount", 0) or 0),
            "pending": int(stats.get("pendingCount", 0) or 0),
            "disapproved": int(stats.get("disapprovedCount", 0) or 0),
            "expiring": int(stats.get("expiringCount", 0) or 0),
            "issues": issues[:25],
            "more_issues": max(0, len(issues) - 25),
        })
    groups.sort(key=lambda g: (g["reporting_context"] == "UNSPECIFIED", -g["disapproved"],
                               g["reporting_context"], g["country"] or ""))
    by_destination: Dict[str, int] = {}
    for g in groups:
        if g["reporting_context"] != "UNSPECIFIED":
            key = f'{g["reporting_context"]} {g["country"] or ""}'.strip()
            by_destination[key] = max(by_destination.get(key, 0), g["disapproved"])
    return {
        "account_id": aid,
        "filters": {"country": country or None, "reporting_context": reporting_context or None},
        "groups": groups,
        "disapproved_by_destination": by_destination,
        "note": "Counts are per destination (reporting context) + country; one product can be "
                "disapproved in several destinations, so do not add them up. "
                "UNSPECIFIED = groups Google returns without a destination name.",
    }


def _price(p: Optional[Dict[str, Any]]) -> Optional[str]:
    if not p or not p.get("amountMicros"):
        return None
    return f'{int(p["amountMicros"]) / 1_000_000:.2f} {p.get("currencyCode", "")}'.strip()


def format_product(p: Dict[str, Any]) -> Dict[str, Any]:
    attrs = p.get("productAttributes", {}) or {}
    status = p.get("productStatus", {}) or {}
    dests = []
    disapproved = False
    for d in status.get("destinationStatuses", []) or []:
        entry = {"reporting_context": d.get("reportingContext")}
        for key, out in (("approvedCountries", "approved"), ("pendingCountries", "pending"), ("disapprovedCountries", "disapproved")):
            if d.get(key):
                entry[out] = d[key]
        disapproved = disapproved or bool(d.get("disapprovedCountries"))
        dests.append(entry)
    issues = [
        {
            "severity": i.get("severity"),
            "description": i.get("description"),
            "attribute": i.get("attribute"),
            "reporting_context": i.get("reportingContext"),
            "countries": i.get("applicableCountries"),
            "resolution": i.get("resolution"),
        }
        for i in status.get("itemLevelIssues", []) or []
    ]
    return {
        "offer_id": p.get("offerId"),
        "title": attrs.get("title"),
        "price": _price(attrs.get("price")),
        "sale_price": _price(attrs.get("salePrice")),
        "availability": attrs.get("availability"),
        "link": attrs.get("link"),
        "brand": attrs.get("brand"),
        "feed_label": p.get("feedLabel"),
        "language": p.get("contentLanguage"),
        "disapproved": disapproved,
        "destinations": dests,
        "issues": issues,
    }


def list_products(account_id: str, status: str = "all", limit: int = 50, page_token: str = "") -> Dict[str, Any]:
    aid = clean_account_id(account_id)
    status = (status or "all").strip().lower()
    if status not in ("all", "disapproved", "issues"):
        raise MerchantInputError("status must be 'all', 'disapproved' or 'issues'.")
    limit = max(1, min(int(50 if limit is None else limit), MAX_PRODUCTS))

    if status == "all":
        data = _get(f"products/v1/accounts/{aid}/products", {"pageSize": limit, "pageToken": page_token})
        rows = [format_product(p) for p in data.get("products", [])]
        return {
            "account_id": aid, "status_filter": status, "count": len(rows),
            "next_page_token": data.get("nextPageToken"), "products": rows,
        }

    matched, scanned, token, truncated = [], 0, page_token or None, False
    while True:
        data = _get(f"products/v1/accounts/{aid}/products", {"pageSize": PAGE_SIZE, "pageToken": token})
        batch = data.get("products", [])
        scanned += len(batch)
        for p in batch:
            row = format_product(p)
            if (status == "disapproved" and row["disapproved"]) or (status == "issues" and row["issues"]):
                matched.append(row)
        token = data.get("nextPageToken")
        if not token:
            break
        if scanned >= PRODUCT_SCAN_CAP:
            truncated = True
            break
    reasons = Counter()
    for r in matched:  # count each product once per reason
        reasons.update({
            i["description"] for i in r["issues"]
            if status != "disapproved" or i["severity"] == "DISAPPROVED"
        })
    return {
        "account_id": aid,
        "status_filter": status,
        "products_scanned": scanned,
        "scan_truncated": truncated,
        "matched": len(matched),
        "top_reasons": [{"reason": k, "products": v} for k, v in reasons.most_common(15)],
        "count": min(limit, len(matched)),
        "products": matched[:limit],
        "next_page_token": token if truncated else None,
    }


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


def _run(fn, *args, **kwargs) -> str:
    from fastmcp.exceptions import ToolError

    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except (MerchantInputError, google_rest.GoogleApiError) as e:
        raise ToolError(str(e))


def register(mcp) -> None:
    from mcp.types import ToolAnnotations

    ro = ToolAnnotations(readOnlyHint=True)

    @mcp.tool(annotations=ro)
    def merchant_list_accounts(parent_account_id: str = "") -> str:
        """List Google Merchant Center accounts this login can access (read-only).

        Call this first to get the account_id for the other merchant_ tools.

        Args:
            parent_account_id: Optional advanced (multi-client) account ID; then its
                sub-accounts are listed instead.
        """
        return _run(list_accounts, parent_account_id)

    @mcp.tool(annotations=ro)
    def merchant_account_issues(account_id: str, language: str = "nl") -> str:
        """Account-level Merchant Center problems (e.g. suspensions, policy, website, shipping).

        Args:
            account_id: Merchant Center account ID (from merchant_list_accounts).
            language: Language for the issue texts (BCP-47, default "nl").

        Returns issues sorted by severity (CRITICAL stops products from serving),
        with the affected destinations (e.g. SHOPPING_ADS, FREE_LISTINGS) and countries.
        """
        return _run(account_issues, account_id, language)

    @mcp.tool(annotations=ro)
    def merchant_product_status_summary(
        account_id: str, country: str = "", reporting_context: str = ""
    ) -> str:
        """Summary of product statuses and the reasons products are disapproved or limited.

        Best first tool for "why are my Shopping products not showing?". Per destination
        (reporting context) and country: active/pending/disapproved/expiring counts and the
        item-level issues with the number of affected products, disapprovals first.

        Args:
            account_id: Merchant Center account ID (from merchant_list_accounts).
            country: Optional CLDR country code filter (e.g. "BE", "NL").
            reporting_context: Optional filter, e.g. "SHOPPING_ADS", "FREE_LISTINGS".
        """
        return _run(product_status_summary, account_id, country, reporting_context)

    @mcp.tool(annotations=ro)
    def merchant_list_products(
        account_id: str, status: str = "all", limit: int = 50, page_token: str = ""
    ) -> str:
        """List Merchant Center products with their approval status and issues.

        Args:
            account_id: Merchant Center account ID (from merchant_list_accounts).
            status: "all" (paged), "disapproved" (only products disapproved somewhere) or
                "issues" (any product with an issue). Filters scan up to 5000 products.
            limit: Max products returned (default 50, max 250).
            page_token: For status "all": next_page_token from the previous call.

        Each product: offer_id, title, price, availability, link, destinations
        (approved/pending/disapproved countries) and issues (severity, description,
        attribute, resolution). Filtered calls also return top_reasons.
        """
        return _run(list_products, account_id, status, limit, page_token)
