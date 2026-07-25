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

Runs in streamable-http mode with automatic Accept header injection
for clients (like ClickUp) that don't send it.
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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.middleware import Middleware

logger = logging.getLogger(__name__)


class AcceptHeaderMiddleware(BaseHTTPMiddleware):
    """Injects Accept: text/event-stream if the client doesn't send it.

    FastMCP's streamable-http transport requires this header, but some
    MCP clients (like ClickUp) don't include it.
    """

    async def dispatch(self, request: Request, call_next):
        accept = request.headers.get("accept", "")
        if "text/event-stream" not in accept:
            # Mutate the scope headers to include the required Accept header
            headers = dict(request.scope["headers"])
            new_headers = []
            found_accept = False
            for key, value in request.scope["headers"]:
                if key == b"accept":
                    new_headers.append((key, b"text/event-stream, " + value))
                    found_accept = True
                else:
                    new_headers.append((key, value))
            if not found_accept:
                new_headers.append((b"accept", b"text/event-stream"))
            request.scope["headers"] = new_headers
        return await call_next(request)


def run_server() -> None:
    port = int(os.environ.get("PORT", "8080"))

    logger.info(f"Starting Google Ads MCP server on port {port}")

    # Run in streamable-http mode for remote access
    mcp.run(
        transport="streamable-http",
        port=port,
        host="0.0.0.0",
        middleware=[Middleware(AcceptHeaderMiddleware)],
    )


if __name__ == "__main__":
    run_server()
