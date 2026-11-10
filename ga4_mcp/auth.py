"""Google Analytics 4 authentication via refresh token.

Uses the shared Google login (ads_mcp/user_credentials.py): GSC OAuth client,
refresh token GA4_REFRESH_TOKEN (if set) -> GOOGLE_REFRESH_TOKEN -> GSC_REFRESH_TOKEN.
"""

import logging

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.admin_v1beta import AnalyticsAdminServiceClient

from ads_mcp.user_credentials import get_user_credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

_data_client_cache = None
_admin_client_cache = None


def _get_credentials():
    """Shared Google login; a GA4-specific token (GA4_REFRESH_TOKEN) is tried first if set.

    Note: GA4_CLIENT_ID / GA4_CLIENT_SECRET are no longer used; the shared
    GSC OAuth client is always used.
    """
    return get_user_credentials(SCOPES, extra_token_env=("GA4_REFRESH_TOKEN",))


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
