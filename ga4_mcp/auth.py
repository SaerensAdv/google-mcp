"""Google Analytics 4 authentication via refresh token.

Reuses GSC credentials by default (same OAuth app, same token).
Falls back to GA4-specific env vars if set.

Looks for: GA4_CLIENT_ID / GA4_CLIENT_SECRET / GA4_REFRESH_TOKEN
Fallback:  GSC_CLIENT_ID / GSC_CLIENT_SECRET / GSC_REFRESH_TOKEN
"""

import os
import logging

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.admin_v1beta import AnalyticsAdminServiceClient

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

_data_client_cache = None
_admin_client_cache = None


def _get_credentials():
    """Build and refresh OAuth2 credentials from env vars."""
    client_id = os.environ.get("GA4_CLIENT_ID") or os.environ.get("GSC_CLIENT_ID")
    client_secret = os.environ.get("GA4_CLIENT_SECRET") or os.environ.get("GSC_CLIENT_SECRET")
    refresh_token = os.environ.get("GA4_REFRESH_TOKEN") or os.environ.get("GSC_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "Missing GA4/GSC credentials. Set GA4_CLIENT_ID, GA4_CLIENT_SECRET, "
            "GA4_REFRESH_TOKEN (or their GSC_ equivalents) in Replit Secrets."
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
    return creds


def get_ga4_data_client():
    """Return an authenticated GA4 Data API client, cached after first call."""
    global _data_client_cache
    if _data_client_cache is not None:
        return _data_client_cache

    creds = _get_credentials()
    _data_client_cache = BetaAnalyticsDataClient(credentials=creds)
    logger.info("GA4 Data API client authenticated successfully")
    return _data_client_cache


def get_ga4_admin_client():
    """Return an authenticated GA4 Admin API client, cached after first call."""
    global _admin_client_cache
    if _admin_client_cache is not None:
        return _admin_client_cache

    creds = _get_credentials()
    _admin_client_cache = AnalyticsAdminServiceClient(credentials=creds)
    logger.info("GA4 Admin API client authenticated successfully")
    return _admin_client_cache
