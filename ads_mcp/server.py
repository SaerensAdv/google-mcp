"""Minimal MCP server for ClickUp compatibility testing.

Uses mcp.run() directly - no ASGI wrapper.
"""

from fastmcp import FastMCP
import os
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

mcp = FastMCP("Google Ads")


@mcp.tool()
def ping() -> str:
    """Returns pong to verify the server is working."""
    return "pong"


@mcp.tool()
def list_customers() -> str:
    """Lists accessible Google Ads customer IDs."""
    import ads_mcp.utils as utils
    from google.ads.googleads.v24.services.types.customer_service import (
        ListAccessibleCustomersResponse,
    )

    ga_service = utils.get_googleads_service("CustomerService")
    accessible_customers: ListAccessibleCustomersResponse = (
        ga_service.list_accessible_customers()
    )
    customer_ids = [
        cust_rn.removeprefix("customers/")
        for cust_rn in accessible_customers.resource_names
    ]
    return ", ".join(customer_ids)


def run_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting minimal Google Ads MCP on port {port}")

    mcp.run(
        transport="streamable-http",
        port=port,
        host="0.0.0.0",
    )


if __name__ == "__main__":
    run_server()
