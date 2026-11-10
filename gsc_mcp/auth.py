"""Google Search Console authentication via refresh token.

Uses the shared Google login (ads_mcp/user_credentials.py):
GSC_CLIENT_ID / GSC_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN, falling back to GSC_REFRESH_TOKEN.
"""

import logging

from googleapiclient.discovery import build

from ads_mcp.user_credentials import get_user_credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

_service_cache = None


def get_gsc_service():
    """Return an authenticated Search Console service, cached after first call."""
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    creds = get_user_credentials(SCOPES)
    _service_cache = build(
        "searchconsole", "v1", credentials=creds, cache_discovery=False
    )
    logger.info("GSC service authenticated successfully")
    return _service_cache
