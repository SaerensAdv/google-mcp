"""Google Search Console authentication via refresh token.

Expects these environment variables in Replit Secrets:
- GSC_CLIENT_ID
- GSC_CLIENT_SECRET
- GSC_REFRESH_TOKEN

Optionally reuses the same OAuth app as Google Ads if scopes allow it.
"""

import os
import logging

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

_service_cache = None


def get_gsc_service():
    """Return an authenticated Search Console service, cached after first call."""
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    client_id = os.environ.get("GSC_CLIENT_ID")
    client_secret = os.environ.get("GSC_CLIENT_SECRET")
    refresh_token = os.environ.get("GSC_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "Missing GSC credentials. Set GSC_CLIENT_ID, GSC_CLIENT_SECRET, "
            "and GSC_REFRESH_TOKEN in Replit Secrets."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )

    creds.refresh(Request())
    _service_cache = build(
        "searchconsole", "v1", credentials=creds, cache_discovery=False
    )
    logger.info("GSC service authenticated successfully")
    return _service_cache
