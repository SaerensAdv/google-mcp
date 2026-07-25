"""Minimal MCP server for ClickUp compatibility testing."""

from fastmcp import FastMCP
import os
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

mcp = FastMCP("Google Ads")


@mcp.tool()
def ping() -> str:
    """Test tool that returns a simple greeting."""
    return "pong - Google Ads MCP is working"


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
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting minimal Google Ads MCP on port {port}")

    app = mcp.http_app(transport="streamable-http")

    class InjectAcceptHeader:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                headers = list(scope.get("headers", []))
                has_sse = any(
                    k == b"accept" and b"text/event-stream" in v
                    for k, v in headers
                )
                if not has_sse:
                    headers = [(k, v) for k, v in headers if k != b"accept"]
                    headers.append((b"accept", b"text/event-stream, */*"))
                    scope = dict(scope, headers=headers)
            await self.app(scope, receive, send)

    wrapped = InjectAcceptHeader(app)
    uvicorn.run(wrapped, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    run_server()
