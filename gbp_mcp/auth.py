"""Google Business Profile auth + read-only REST access.

Uses the shared Google login (ads_mcp/user_credentials.py), same as GSC/GA4:
GSC OAuth client + GOOGLE_REFRESH_TOKEN, falling back to GSC_REFRESH_TOKEN.

Google has no read-only Business Profile scope: `business.manage` also allows
edits. Read-only is enforced here: this module only exposes GET.

Google errors are translated into short, actionable messages, in particular:
  - an API switched off in the Google Cloud project (SERVICE_DISABLED)
  - quota 0 = Google has not (yet) approved GBP API access for the project
  - no permission on an account/location
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from google.auth.transport.requests import AuthorizedSession

from ads_mcp.google_rest import GoogleApiError, translate_error
from ads_mcp.user_credentials import MissingGooglePermission, get_user_credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/business.manage"]
TIMEOUT_SECONDS = 30

# service key -> (base URL, API name as shown in Google Cloud Console)
SERVICES = {
    "accounts": ("https://mybusinessaccountmanagement.googleapis.com/v1", "My Business Account Management API"),
    "info": ("https://mybusinessbusinessinformation.googleapis.com/v1", "My Business Business Information API"),
    "performance": ("https://businessprofileperformance.googleapis.com/v1", "Business Profile Performance API"),
    "reviews": ("https://mybusiness.googleapis.com/v4", "Google My Business API"),
}

ACCESS_REQUEST_URL = "https://developers.google.com/my-business/content/prereqs#request-access"
REQUIRED_APIS = ", ".join(label for _, label in SERVICES.values())


def _session() -> AuthorizedSession:
    return AuthorizedSession(get_user_credentials(SCOPES))


def _error_info(payload: Any) -> Dict[str, Any]:
    """First ErrorInfo-like detail (reason + metadata) of a Google error payload."""
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    for detail in err.get("details", []) or []:
        if isinstance(detail, dict) and (detail.get("reason") or detail.get("metadata")):
            return detail
    return {}


def _project(meta: Dict[str, Any]) -> str:
    consumer = str(meta.get("consumer") or meta.get("containerInfo") or "")
    return consumer.rsplit("/", 1)[-1] if consumer else "of the Google login"


def translate_gbp_error(service: str, status: int, payload: Any) -> GoogleApiError:
    label = SERVICES[service][1]
    info = _error_info(payload)
    reason = info.get("reason", "")
    meta = info.get("metadata") or {}
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = str(err.get("message") or "")

    if reason == "SERVICE_DISABLED" or "has not been used in project" in message:
        url = meta.get("activationUrl", "")
        text = (
            f"Google Business Profile: the {label} is switched off in Google Cloud project "
            f"{_project(meta)}."
        )
        if url:
            text += f" Enable it here: {url} ."
        text += (
            f" The gbp_ tools need all of these APIs enabled: {REQUIRED_APIS}."
            " Wait a few minutes after enabling, then retry."
        )
        return GoogleApiError(text, status, "SERVICE_DISABLED")

    if status == 429 and str(meta.get("quota_limit_value", "")) == "0":
        return GoogleApiError(
            f"Google Business Profile: Google has not (yet) approved API access for Google Cloud project "
            f"{_project(meta)} (the {label} has a quota of 0). Request access via the GBP API access "
            f"request form ({ACCESS_REQUEST_URL}) and retry once Google has approved it.",
            status, "ACCESS_NOT_APPROVED",
        )

    if status == 429:
        return GoogleApiError(
            f"Google Business Profile: rate limit of the {label} reached, wait a minute and retry.",
            status, "RATE_LIMIT",
        )

    if status == 403 and reason != "ACCESS_TOKEN_SCOPE_INSUFFICIENT" and "scope" not in message.lower():
        return GoogleApiError(
            "Google Business Profile: the Google login has no access to this account or location. "
            "Check that the login is an owner or manager of the Business Profile, and take the IDs "
            "from gbp_list_accounts / gbp_list_locations.",
            status, "PERMISSION_DENIED",
        )

    if status == 404:
        return GoogleApiError(
            "Google Business Profile: account or location not found. Take the IDs from "
            "gbp_list_accounts / gbp_list_locations.",
            status, "NOT_FOUND",
        )

    return translate_error(f"Google Business Profile ({label})", status, payload)


def get_json(service: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET `<service base>/<path>` with the shared login. Raises GoogleApiError on failure."""
    base, label = SERVICES[service]
    try:
        session = _session()
    except MissingGooglePermission as e:
        raise GoogleApiError(f"Google Business Profile: {e}", 0, "SCOPE") from None
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    resp = session.get(f"{base}/{path}", params=clean, timeout=TIMEOUT_SECONDS)
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text[:500]
    if resp.status_code >= 400:
        err = translate_gbp_error(service, resp.status_code, payload)
        logger.warning("GBP %s GET %s -> %s %s", label, path.split("?")[0], resp.status_code, err.reason)
        raise err
    return payload if isinstance(payload, dict) else {}
