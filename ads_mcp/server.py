# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Entry point for the MCP server.

Runs in streamable-http mode. All tools registered flat on the main instance.
"""

from ads_mcp.coordinator import mcp
from mcp.types import ToolAnnotations
from typing import Any, Dict, List
import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException
from fastmcp.exceptions import ToolError
from google.ads.googleads.v24.services.types.customer_service import (
    ListAccessibleCustomersResponse,
)

# The following imports register resources with the `mcp` object.
from ads_mcp.resources import (
    discovery,
    metrics,
    release_notes,
    segments,
)  # noqa: F401

import os
import logging

logger = logging.getLogger(__name__)


# ─── Tools registered directly on mcp ───────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_accessible_customers() -> List[str]:
    """Returns ids of customers directly accessible by the user authenticating the call.

    Use this tool first to discover available customer IDs if the user hasn't
    provided one. Most other tools require a valid customer ID as input.

    Returns:
        List[str]: A list of customer IDs.
    """
    ga_service = utils.get_googleads_service("CustomerService")
    accessible_customers: ListAccessibleCustomersResponse = (
        ga_service.list_accessible_customers()
    )
    return [
        cust_rn.removeprefix("customers/")
        for cust_rn in accessible_customers.resource_names
    ]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def search(
    customer_id: str,
    fields: List[str],
    resource: str,
    conditions: List[str] = [],
    orderings: List[str] = [],
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    """Fetches data from the Google Ads API using GAQL.

    Args:
        customer_id: The customer ID (digits only, no hyphens)
        fields: The fields to select (e.g. ['campaign.name', 'metrics.clicks'])
        resource: The resource to query (e.g. 'campaign', 'ad_group')
        conditions: WHERE conditions combined with AND
        orderings: ORDER BY clauses
        limit: Max rows to return

    Hints:
        - Use get_resource_metadata to discover valid fields for a resource
        - Dates must be YYYY-MM-DD format
        - customer_id should be digits only (no hyphens)
        - change_event queries require LIMIT <= 10000
    """
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_resource_metadata(resource_name: str) -> Dict[str, Any]:
    """Retrieves selectable, filterable, and sortable fields for a Google Ads resource.

    Use this to discover valid fields before constructing a search query.
    Returns metrics and segments compatible with the resource too.

    Args:
        resource_name: The resource name (e.g. 'campaign', 'ad_group', 'search_term_view')
    """
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


# ─── Server entry point ─────────────────────────────────────────────────────

def run_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting Google Ads MCP server on port {port}")

    mcp.run(
        transport="streamable-http",
        port=port,
        host="0.0.0.0",
    )


if __name__ == "__main__":
    run_server()
