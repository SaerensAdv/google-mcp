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

Runs in streamable-http mode. Middleware temporarily disabled for
ClickUp MCP validation testing.
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


def run_server() -> None:
    port = int(os.environ.get("PORT", "8080"))

    logger.info(f"Starting Google Ads MCP server on port {port}")

    # Run in streamable-http mode for remote access
    # API key middleware disabled temporarily for connection validation
    mcp.run(
        transport="streamable-http",
        port=port,
        host="0.0.0.0",
    )


if __name__ == "__main__":
    run_server()
