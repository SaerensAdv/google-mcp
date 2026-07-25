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

Runs in streamable-http mode with optional API key authentication.
Set MCP_API_KEY env var to require X-API-Key header on all requests.
"""

from ads_mcp.coordinator import mcp

# The following imports are necessary to register the resources with the `mcp`
# object, even though they are not directly used in this file.
# Tools are loaded dynamically via reflection in coordinator.py.
from ads_mcp.resources import (
    discovery,
    metrics,
    release_notes,
    segments,
)  # noqa: F401


import os
import logging

logger = logging.getLogger(__name__)


def _get_asgi_app():
    """Wraps the MCP ASGI app with API key middleware if MCP_API_KEY is set."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    api_key = os.environ.get("MCP_API_KEY")

    class APIKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if api_key:
                provided_key = request.headers.get("X-API-Key")
                if provided_key != api_key:
                    return JSONResponse(
                        {"error": "Unauthorized: invalid or missing API key"},
                        status_code=401,
                    )
            return await call_next(request)

    return APIKeyMiddleware


def run_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    api_key = os.environ.get("MCP_API_KEY")

    if api_key:
        logger.info("API key authentication enabled")
    else:
        logger.warning(
            "No MCP_API_KEY set - server is accessible without authentication!"
        )

    # Always run in streamable-http mode for remote access
    mcp.run(
        transport="streamable-http",
        port=port,
        host="0.0.0.0",
        http_middlewares=[_get_asgi_app()],
        uvicorn_config={"access_log": False},
    )


if __name__ == "__main__":
    run_server()
