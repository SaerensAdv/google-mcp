"""Minimal read-only REST helper for Google APIs using the shared user login.

Used by the Merchant Center and Tag Manager tools. Only GET is exposed on
purpose: these tools must never change anything.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Sequence

from google.auth.transport.requests import AuthorizedSession

from ads_mcp.user_credentials import MissingGooglePermission, get_user_credentials

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


class GoogleApiError(RuntimeError):
    """A Google API error translated into a short, actionable message."""

    def __init__(self, message: str, status: int = 0, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


def _session(scopes: Sequence[str]) -> AuthorizedSession:
    return AuthorizedSession(get_user_credentials(scopes))


def translate_error(api_label: str, status: int, payload: Any) -> GoogleApiError:
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = str(err.get("message") or payload or "")[:500]
    reason = ""
    activation_url = ""
    for detail in err.get("details", []) or []:
        if not isinstance(detail, dict):
            continue
        reason = reason or detail.get("reason", "")
        meta = detail.get("metadata") or {}
        activation_url = activation_url or meta.get("activationUrl", "")
    if not activation_url:
        m = re.search(r"https://console\.developers\.google\.com/apis/api/\S+?overview\?project=\d+", message)
        activation_url = m.group(0) if m else ""

    if reason == "SERVICE_DISABLED" or "has not been used in project" in message:
        text = f"The {api_label} is not enabled in the Google Cloud project of the Google login."
        if activation_url:
            text += f" Enable it here, wait a few minutes and retry: {activation_url}"
        return GoogleApiError(text, status, "SERVICE_DISABLED")
    if reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT" or "insufficient authentication scopes" in message.lower():
        return GoogleApiError(
            f"The Google login lacks the permission for the {api_label}. "
            "Log in again with all scopes (see replit.md, 'Google login') and update GOOGLE_REFRESH_TOKEN.",
            status, "SCOPE",
        )
    if status == 404:
        return GoogleApiError(f"{api_label}: not found. {message}", status, "NOT_FOUND")
    if status == 429:
        return GoogleApiError(f"{api_label}: rate limit reached, wait a minute and retry. {message}", status, "RATE_LIMIT")
    if status in (401, 403):
        return GoogleApiError(
            f"{api_label}: access denied. The Google login probably has no access to this account. {message}",
            status, reason or "PERMISSION_DENIED",
        )
    return GoogleApiError(f"{api_label} error ({status}): {message}", status, reason)


def get_json(
    api_label: str,
    url: str,
    scopes: Sequence[str],
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """GET `url` with the shared login; returns parsed JSON or raises GoogleApiError."""
    try:
        session = _session(scopes)
    except MissingGooglePermission as e:
        raise GoogleApiError(f"{api_label}: {e}", 0, "SCOPE") from None
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    resp = session.get(url, params=clean, timeout=TIMEOUT_SECONDS)
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text[:500]
    if resp.status_code >= 400:
        err = translate_error(api_label, resp.status_code, payload)
        logger.warning("%s GET %s -> %s %s", api_label, url.split("?")[0], resp.status_code, err.reason)
        raise err
    return payload if isinstance(payload, dict) else {}
