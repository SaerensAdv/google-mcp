"""Unified MCP server for Google Ads + Google Search Console + Google Analytics 4 + PageSpeed Insights.

Tools:
  Google Ads: ping, list_customers, search, get_resource_metadata
  GSC: gsc_list_properties, gsc_search_analytics, gsc_advanced_search_analytics,
       gsc_performance_overview, gsc_compare_periods, gsc_search_by_page, gsc_inspect_url
  GA4: ga4_list_properties, ga4_run_report, ga4_traffic_overview,
       ga4_traffic_sources, ga4_top_pages, ga4_conversions, ga4_landing_pages
  PSI: psi_analyze
"""

from fastmcp import FastMCP
from typing import Any, Dict, List
import os
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

mcp = FastMCP("Google MCP")

# ---------------------------------------------------------------------------
# Google Ads tools
# ---------------------------------------------------------------------------


@mcp.tool()
def ping() -> str:
    """Returns pong to verify the server is working."""
    return "pong"


@mcp.tool()
def list_customers() -> List[str]:
    """Lists accessible Google Ads customer IDs.

    Use this first to discover available customer IDs.
    Returns a list of customer ID strings (digits only).
    """
    import ads_mcp.utils as utils
    from google.ads.googleads.v24.services.types.customer_service import (
        ListAccessibleCustomersResponse,
    )

    ga_service = utils.get_googleads_service("CustomerService")
    accessible_customers: ListAccessibleCustomersResponse = (
        ga_service.list_accessible_customers()
    )
    return [
        cust_rn.removeprefix("customers/")
        for cust_rn in accessible_customers.resource_names
    ]


@mcp.tool()
def search(
    customer_id: str,
    fields: List[str],
    resource: str,
    conditions: List[str] = [],
    orderings: List[str] = [],
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Fetches data from the Google Ads API using GAQL (Google Ads Query Language).

    Args:
        customer_id: The customer ID (digits only, no hyphens). Use list_customers to find IDs.
        fields: The fields to select (e.g. ['campaign.name', 'metrics.clicks', 'metrics.cost_micros'])
        resource: The resource to query (e.g. 'campaign', 'ad_group', 'search_term_view', 'keyword_view')
        conditions: WHERE conditions combined with AND (e.g. ['segments.date DURING LAST_30_DAYS'])
        orderings: ORDER BY clauses (e.g. ['metrics.clicks DESC'])
        limit: Max rows to return (default 100)

    Common resources:
        - campaign: campaign performance
        - ad_group: ad group performance
        - ad_group_ad: individual ad performance
        - keyword_view: keyword performance
        - search_term_view: actual search terms that triggered ads
        - geographic_view: performance by location
        - landing_page_view: landing page performance

    Hints:
        - Use get_resource_metadata to discover valid fields for a resource
        - Dates: use segments.date with DURING LAST_7_DAYS, LAST_30_DAYS, THIS_MONTH, LAST_MONTH, or specific dates like 'segments.date BETWEEN "2026-01-01" AND "2026-01-31"'
        - cost_micros is in millionths: divide by 1_000_000 to get actual currency amount
        - customer_id must be digits only (remove hyphens)
    """
    import ads_mcp.utils as utils
    from google.ads.googleads.errors import GoogleAdsException
    from fastmcp.exceptions import ToolError

    ga_service = utils.get_googleads_service("GoogleAdsService")

    query_parts = [f"SELECT {','.join(fields)} FROM {resource}"]
    if conditions:
        query_parts.append(f" WHERE {' AND '.join(conditions)}")
    if orderings:
        query_parts.append(f" ORDER BY {','.join(orderings)}")
    if limit:
        query_parts.append(f" LIMIT {limit}")
    query_parts.append(" PARAMETERS omit_unselected_resource_names=true")

    query = "".join(query_parts)
    logger.info(f"GAQL: {query}")

    try:
        query_result = ga_service.search_stream(
            customer_id=customer_id, query=query
        )
        final_output: List = []
        for batch in query_result:
            for row in batch.results:
                final_output.append(
                    utils.format_output_row(row, batch.field_mask.paths)
                )
        return final_output
    except GoogleAdsException as ex:
        error_msgs = [
            f"Google Ads API Error: {error.message}"
            for error in ex.failure.errors
        ]
        raise ToolError(
            f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)
        )


@mcp.tool()
def get_resource_metadata(resource_name: str) -> Dict[str, Any]:
    """Retrieves selectable, filterable, and sortable fields for a Google Ads resource.

    Use this to discover which fields you can use in the search tool.
    Returns metrics and segments compatible with the resource too.

    Args:
        resource_name: The resource name (e.g. 'campaign', 'ad_group', 'search_term_view')
    """
    import ads_mcp.utils as utils

    ga_service = utils.get_googleads_service("GoogleAdsFieldService")
    request = utils.get_googleads_type("SearchGoogleAdsFieldsRequest")

    selectable = set()
    filterable = set()
    sortable = set()

    # Get resource attributes
    request.query = f"SELECT name, selectable, filterable, sortable WHERE name LIKE '{resource_name}.%' AND category = 'ATTRIBUTE'"
    try:
        for field in ga_service.search_google_ads_fields(request=request):
            if field.selectable:
                selectable.add(field.name)
            if field.filterable:
                filterable.add(field.name)
            if field.sortable:
                sortable.add(field.name)
    except Exception as e:
        logger.warning(f"Attributes query failed: {e}")

    # Get compatible metrics and segments
    request.query = f"SELECT name, selectable, filterable, sortable WHERE selectable_with CONTAINS ANY('{resource_name}')"
    try:
        for field in ga_service.search_google_ads_fields(request=request):
            if field.selectable:
                selectable.add(field.name)
            if field.filterable:
                filterable.add(field.name)
            if field.sortable:
                sortable.add(field.name)
    except Exception as e:
        logger.warning(f"Metrics/segments query failed: {e}")

    return {
        "resource": resource_name,
        "selectable": sorted(list(selectable)),
        "filterable": sorted(list(filterable)),
        "sortable": sorted(list(sortable)),
    }


# ---------------------------------------------------------------------------
# Google Search Console tools (registered from gsc_mcp module)
# ---------------------------------------------------------------------------

try:
    from gsc_mcp.tools import register as register_gsc_tools
    register_gsc_tools(mcp)
    logger.info("GSC tools registered successfully")
except ImportError as e:
    logger.warning(f"GSC tools not loaded (missing dependencies): {e}")
except Exception as e:
    logger.warning(f"GSC tools failed to register: {e}")


# ---------------------------------------------------------------------------
# Google Analytics 4 tools (registered from ga4_mcp module)
# ---------------------------------------------------------------------------

try:
    from ga4_mcp.tools import register as register_ga4_tools
    register_ga4_tools(mcp)
    logger.info("GA4 tools registered successfully")
except ImportError as e:
    logger.warning(f"GA4 tools not loaded (missing dependencies): {e}")
except Exception as e:
    logger.warning(f"GA4 tools failed to register: {e}")


# ---------------------------------------------------------------------------
# PageSpeed Insights tools (registered from psi_mcp module)
# ---------------------------------------------------------------------------

try:
    from psi_mcp.tools import register as register_psi_tools
    register_psi_tools(mcp)
    logger.info("PSI tools registered successfully")
except ImportError as e:
    logger.warning(f"PSI tools not loaded (missing dependencies): {e}")
except Exception as e:
    logger.warning(f"PSI tools failed to register: {e}")


# ---------------------------------------------------------------------------
# Server entrypoint
# ---------------------------------------------------------------------------


def run_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting Google MCP server on port {port}")

    mcp.run(
        transport="streamable-http",
        port=port,
        host="0.0.0.0",
    )


if __name__ == "__main__":
    run_server()
