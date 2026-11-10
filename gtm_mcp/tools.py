"""Google Tag Manager tools (Tag Manager API v2, read-only).

All tools are prefixed with gtm_. Call register(mcp) to add them to an existing
FastMCP instance. Uses the shared Google login with the tagmanager.readonly
scope and only GET requests.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ads_mcp import google_rest

logger = logging.getLogger(__name__)

API_LABEL = "Tag Manager API"
BASE = "https://tagmanager.googleapis.com/tagmanager/v2"
SCOPES = ["https://www.googleapis.com/auth/tagmanager.readonly"]

MAX_ITEMS = 300

# Built-in GTM tag types -> readable name and platform.
TAG_TYPES = {
    "awct": ("Google Ads Conversion Tracking", "google_ads"),
    "sp": ("Google Ads Remarketing", "google_ads"),
    "awud": ("Google Ads User-Provided Data Event", "google_ads"),
    "gclidw": ("Conversion Linker", "google_ads"),
    "googtag": ("Google Tag", None),  # platform from tagId (G- / AW-)
    "gaawc": ("GA4 Configuration (legacy)", "ga4"),
    "gaawe": ("GA4 Event", "ga4"),
    "dest_ga": ("Google Tag (GA4 destination)", "ga4"),
    "ua": ("Universal Analytics (discontinued)", "universal_analytics"),
    "flc": ("Floodlight Counter", "floodlight"),
    "fls": ("Floodlight Sales", "floodlight"),
    "bzi": ("LinkedIn Insight", "linkedin"),
    "baut": ("Microsoft Advertising UET", "microsoft_ads"),
    "hjtc": ("Hotjar Tracking Code", "hotjar"),
    "html": ("Custom HTML", None),
    "img": ("Custom Image", None),
}

# Keywords (in custom HTML or template names) -> platform.
PLATFORM_HINTS = [
    ("meta", ("fbq(", "connect.facebook.net", "facebook pixel", "meta pixel", "facebook", "meta conversion")),
    ("linkedin", ("snap.licdn.com", "_linkedin_partner_id", "linkedin")),
    ("tiktok", ("analytics.tiktok.com", "ttq.", "tiktok")),
    ("microsoft_ads", ("bat.bing.com", "uetq", "microsoft advertising", "bing ads")),
    ("pinterest", ("pintrk", "pinterest")),
    ("snapchat", ("snaptr", "snapchat")),
    ("hotjar", ("static.hotjar.com", "hotjar")),
    ("microsoft_clarity", ("clarity.ms", "microsoft clarity")),
    ("google_ads", ("googleadservices.com", "gtag('config', 'aw-", "gtag(\"config\", \"aw-", "google ads")),
    ("ga4", ("gtag('config', 'g-", "gtag(\"config\", \"g-", "google analytics 4", "ga4")),
]

CONVERSION_PLATFORMS = (
    "google_ads", "ga4", "meta", "linkedin", "tiktok", "microsoft_ads",
    "pinterest", "snapchat", "floodlight", "universal_analytics",
)


class GtmInputError(ValueError):
    pass


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return google_rest.get_json(API_LABEL, f"{BASE}/{path}", SCOPES, params)


def _digits(value: str, name: str) -> str:
    v = str(value or "").strip()
    if not v.isdigit():
        raise GtmInputError(f"{name} must be numeric (use gtm_list_accounts / gtm_list_containers).")
    return v


def _paged(path: str, key: str, max_pages: int = 20) -> List[Dict[str, Any]]:
    items, token = [], None
    for _ in range(max_pages):
        data = _get(path, {"pageToken": token})
        items.extend(data.get(key, []))
        token = data.get("nextPageToken")
        if not token:
            break
    return items


def _params(entity: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for p in entity.get("parameter", []) or []:
        if "value" in p:
            out[p.get("key")] = p["value"]
    return out


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def list_accounts() -> Dict[str, Any]:
    accounts = _paged("accounts", "account")
    rows = [{"account_id": a.get("accountId"), "name": a.get("name")} for a in accounts]
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return {"count": len(rows), "accounts": rows}


def list_containers(account_id: str) -> Dict[str, Any]:
    aid = _digits(account_id, "account_id")
    containers = _paged(f"accounts/{aid}/containers", "container")
    rows = [
        {
            "container_id": c.get("containerId"),
            "public_id": c.get("publicId"),
            "name": c.get("name"),
            "usage": c.get("usageContext"),
            "domains": c.get("domainName"),
            "tag_ids": c.get("tagIds"),
        }
        for c in containers
    ]
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return {"account_id": aid, "count": len(rows), "containers": rows}


def resolve_container(account_id: str, container_id: str) -> tuple:
    """Accepts numeric account+container IDs or a public ID like GTM-ABC123."""
    cid = str(container_id or "").strip()
    if re.fullmatch(r"GTM-[A-Z0-9]+", cid, re.I):
        data = _get("accounts/containers:lookup", {"tagId": cid.upper()})
        aid, num = data.get("accountId"), data.get("containerId")
        if not aid or not num:
            raise GtmInputError(f"Container {cid} not found for this Google login.")
        return aid, num
    return _digits(account_id, "account_id"), _digits(cid, "container_id")


def _platform_for_tag(tag: Dict[str, Any], template_names: Dict[str, str]) -> Optional[str]:
    ttype = tag.get("type", "")
    params = _params(tag)
    if ttype == "googtag":
        tag_id = str(params.get("tagId", "")).upper()
        if tag_id.startswith("AW-"):
            return "google_ads"
        if tag_id.startswith("G-"):
            return "ga4"
        return None
    if ttype in TAG_TYPES and TAG_TYPES[ttype][1]:
        return TAG_TYPES[ttype][1]
    haystack = " ".join([
        str(tag.get("name", "")),
        template_names.get(ttype, ""),
        str(params.get("html", "")) if ttype == "html" else "",
    ]).lower()
    for platform, needles in PLATFORM_HINTS:
        if any(n in haystack for n in needles):
            return platform
    return None


def _tag_key_settings(tag: Dict[str, Any]) -> Dict[str, Any]:
    p = _params(tag)
    ttype = tag.get("type")
    keys = {
        "awct": ("conversionId", "conversionLabel", "conversionValue", "currencyCode", "orderId"),
        "sp": ("conversionId", "conversionLabel"),
        "awud": ("conversionId",),
        "googtag": ("tagId",),
        "gaawe": ("eventName", "measurementIdOverride", "measurementId"),
        "gaawc": ("measurementId",),
        "flc": ("advertiserId", "groupTag", "activityTag"),
        "fls": ("advertiserId", "groupTag", "activityTag"),
        "bzi": ("id",),
        "baut": ("tagId", "eventType"),
    }.get(ttype, ())
    return {k: p[k] for k in keys if k in p}


def live_container_summary(account_id: str = "", container_id: str = "", include_variables: bool = True) -> Dict[str, Any]:
    aid, cid = resolve_container(account_id, container_id)
    version = _get(f"accounts/{aid}/containers/{cid}/versions:live")

    template_names = {}
    for t in version.get("customTemplate", []) or []:
        template_names[f"cvt_{t.get('containerId')}_{t.get('templateId')}"] = t.get("name", "")
        gallery_id = (t.get("galleryReference") or {}).get("galleryTemplateId")
        if gallery_id:
            template_names[f"cvt_{gallery_id}"] = t.get("name", "")

    triggers = version.get("trigger", []) or []
    trigger_names = {t.get("triggerId"): t.get("name") for t in triggers}
    # Built-in triggers are not listed in the version.
    trigger_names.setdefault("2147479553", "All Pages")
    trigger_names.setdefault("2147479572", "Consent Initialization - All Pages")
    trigger_names.setdefault("2147479573", "Initialization - All Pages")

    tags_out = []
    by_platform: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for tag in version.get("tag", []) or []:
        ttype = tag.get("type", "")
        readable = TAG_TYPES.get(ttype, (template_names.get(ttype) or ttype, None))[0]
        platform = _platform_for_tag(tag, template_names)
        firing = [trigger_names.get(t, t) for t in tag.get("firingTriggerId", []) or []]
        blocking = [trigger_names.get(t, t) for t in tag.get("blockingTriggerId", []) or []]
        row = {
            "name": tag.get("name"),
            "type": readable,
            "platform": platform,
            "paused": bool(tag.get("paused", False)),
            "firing_triggers": firing,
        }
        if blocking:
            row["blocking_triggers"] = blocking
        settings = _tag_key_settings(tag)
        if settings:
            row["settings"] = settings
        consent = (tag.get("consentSettings") or {}).get("consentStatus")
        if consent:
            row["consent"] = consent
        if not firing:
            row["warning"] = "no firing trigger (never fires)"
        tags_out.append(row)
        if platform in CONVERSION_PLATFORMS:
            by_platform[platform].append({
                "name": row["name"], "type": readable, "paused": row["paused"],
                "fires_on": firing, **({"settings": settings} if settings else {}),
            })

    triggers_out = []
    for t in triggers:
        entry = {"name": t.get("name"), "type": t.get("type")}
        if t.get("type") == "customEvent":
            for f in t.get("customEventFilter", []) or []:
                vals = [p.get("value") for p in f.get("parameter", []) or [] if p.get("key") == "arg1"]
                if vals:
                    entry["event"] = vals[0]
        if t.get("filter"):
            entry["conditions"] = len(t["filter"])
        triggers_out.append(entry)

    variables_out = [
        {"name": v.get("name"), "type": v.get("type")} for v in version.get("variable", []) or []
    ]

    issues = []
    if any(r["platform"] == "universal_analytics" and not r["paused"] for r in tags_out):
        issues.append("Universal Analytics tags are still active; UA no longer collects data.")
    if any(r.get("warning") for r in tags_out):
        issues.append("Some tags have no firing trigger.")
    if "google_ads" in by_platform and not any(r["type"] == "Conversion Linker" for r in tags_out):
        issues.append("Google Ads tags found but no Conversion Linker tag.")
    paused = [r["name"] for r in tags_out if r["paused"]]
    if paused:
        issues.append(f"{len(paused)} paused tag(s): " + ", ".join(paused[:10]))

    container = version.get("container", {}) or {}
    result = {
        "account_id": aid,
        "container_id": cid,
        "public_id": container.get("publicId"),
        "container_name": container.get("name"),
        "live_version": {"id": version.get("containerVersionId"), "name": version.get("name"),
                         "description": version.get("description")},
        "counts": {"tags": len(tags_out), "triggers": len(triggers_out), "variables": len(variables_out)},
        "conversion_overview": dict(by_platform),
        "attention_points": issues,
        "tags": tags_out[:MAX_ITEMS],
        "triggers": triggers_out[:MAX_ITEMS],
    }
    if include_variables:
        result["variables"] = variables_out[:MAX_ITEMS]
    if any(len(x) > MAX_ITEMS for x in (tags_out, triggers_out, variables_out)):
        result["truncated"] = f"Lists capped at {MAX_ITEMS} items."
    return result


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------


def _run(fn, *args, **kwargs) -> str:
    from fastmcp.exceptions import ToolError

    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except (GtmInputError, google_rest.GoogleApiError) as e:
        raise ToolError(str(e))


def register(mcp) -> None:
    from mcp.types import ToolAnnotations

    ro = ToolAnnotations(readOnlyHint=True)

    @mcp.tool(annotations=ro)
    def gtm_list_accounts() -> str:
        """List Google Tag Manager accounts this login can access (read-only)."""
        return _run(list_accounts)

    @mcp.tool(annotations=ro)
    def gtm_list_containers(account_id: str) -> str:
        """List the containers in a Tag Manager account (ID, public GTM-ID, name, domains).

        Args:
            account_id: Numeric Tag Manager account ID (from gtm_list_accounts).
        """
        return _run(list_containers, account_id)

    @mcp.tool(annotations=ro)
    def gtm_live_container_summary(
        container_id: str, account_id: str = "", include_variables: bool = True
    ) -> str:
        """Readable summary of the LIVE (published) version of a Tag Manager container.

        Use for tracking audits. Returns:
        - conversion_overview: tags per platform (Google Ads, GA4, Meta, LinkedIn, TikTok,
          Microsoft Ads, Pinterest, Snapchat, Floodlight) with the triggers they fire on
        - attention_points: e.g. tags without trigger, paused tags, missing Conversion Linker,
          active Universal Analytics tags
        - tags (type, platform, firing/blocking triggers, key settings such as conversion ID/label,
          GA4 event name, consent status), triggers and variables

        Args:
            container_id: Public ID like "GTM-ABC1234" (no account_id needed) or numeric container ID.
            account_id: Numeric account ID, required only with a numeric container_id.
            include_variables: Include the variable list (default true).
        """
        return _run(live_container_summary, account_id, container_id, include_variables)
