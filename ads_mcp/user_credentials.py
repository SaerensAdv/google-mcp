"""Shared Google user OAuth credentials for GSC, GA4, Merchant Center and Tag Manager.

All of these use the same OAuth client (GSC_CLIENT_ID / GSC_CLIENT_SECRET) and a
refresh token created via the OAuth Playground with that client.

Refresh tokens are tried in this order:
  1. GOOGLE_REFRESH_TOKEN  - newer login with extra permissions (Merchant Center,
                             Tag Manager) on top of the existing ones
  2. GSC_REFRESH_TOKEN     - original login (Search Console, GA4, Business Profile)

The first token that can be refreshed for the requested scopes is used. If the
new token is missing or broken, GSC/GA4 keep working on the original one.

Every service asks only for its own (read-only where possible) scope, so the
access token handed to each API is narrowed to that scope.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"

# Every scope the shared login should have. Used for the re-login instructions.
ALL_SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/business.manage",
    "https://www.googleapis.com/auth/sitemaps",
    "https://www.googleapis.com/auth/content",
    "https://www.googleapis.com/auth/tagmanager.readonly",
]

REFRESH_TOKEN_ENV_ORDER = ("GOOGLE_REFRESH_TOKEN", "GSC_REFRESH_TOKEN")

RELOGIN_HINT = (
    "Log in to Google again via the OAuth Playground with the GSC OAuth client and all "
    "scopes listed in replit.md (section 'Google login'), then store the new refresh token "
    "in the Replit secret GOOGLE_REFRESH_TOKEN and restart/republish."
)

_cache: Dict[Tuple[str, ...], Credentials] = {}
_lock = threading.Lock()


class MissingGooglePermission(RuntimeError):
    """None of the configured logins has the requested scope."""


def _client() -> Tuple[str, str]:
    client_id = os.environ.get("GSC_CLIENT_ID")
    client_secret = os.environ.get("GSC_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise EnvironmentError(
            "Missing GSC_CLIENT_ID / GSC_CLIENT_SECRET in Replit Secrets."
        )
    return client_id, client_secret


def _candidate_tokens(extra_env: Sequence[str] = ()) -> List[Tuple[str, str]]:
    out = []
    for env in tuple(extra_env) + REFRESH_TOKEN_ENV_ORDER:
        value = os.environ.get(env)
        if value and all(value != v for _, v in out):
            out.append((env, value))
    return out


def get_user_credentials(
    scopes: Sequence[str], extra_token_env: Sequence[str] = ()
) -> Credentials:
    """Return refreshed credentials for `scopes`, cached per scope set.

    `extra_token_env` lets a service try its own token first (e.g. GA4_REFRESH_TOKEN).
    Raises MissingGooglePermission when no configured token grants the scopes.
    """
    key = tuple(sorted(scopes)) + tuple(extra_token_env)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached

        client_id, client_secret = _client()
        tokens = _candidate_tokens(extra_token_env)
        if not tokens:
            raise EnvironmentError(
                "No Google refresh token configured. Set GOOGLE_REFRESH_TOKEN "
                "(or GSC_REFRESH_TOKEN) in Replit Secrets."
            )

        failures = []
        for env, refresh_token in tokens:
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri=TOKEN_URI,
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(scopes),
            )
            try:
                creds.refresh(Request())
            except RefreshError as e:
                reason = _refresh_reason(e)
                failures.append(f"{env}: {reason}")
                logger.warning("Google login %s unusable for %s: %s", env, ",".join(scopes), reason)
                continue
            if env != tokens[0][0]:
                logger.info("Using %s for %s (earlier token unusable)", env, ",".join(scopes))
            _cache[key] = creds
            return creds

        raise MissingGooglePermission(
            "No Google login has the required permission ("
            + ", ".join(s.rsplit("/", 1)[-1] for s in scopes)
            + "). Tried: " + "; ".join(failures) + ". " + RELOGIN_HINT
        )


def _refresh_reason(e: RefreshError) -> str:
    text = str(e)
    if "invalid_scope" in text:
        return "permission not granted (invalid_scope)"
    if "invalid_grant" in text:
        return "token expired or revoked (invalid_grant)"
    return text[:200]


def clear_cache() -> None:
    with _lock:
        _cache.clear()
