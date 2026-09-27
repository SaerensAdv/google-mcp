"""Optional inbound authentication for the MCP HTTP endpoint (prepared, OFF by default).

Activate with:
  MCP_INBOUND_AUTH=api_key   (and the MCP_API_KEY secret set)

Clients must then send either ``Authorization: Bearer <MCP_API_KEY>`` or
``X-API-Key: <MCP_API_KEY>``. Implemented as a pure ASGI wrapper (no response
buffering), so streamable-http/SSE responses are passed through untouched.
"""

from __future__ import annotations

import hmac
import json
import logging
import os

logger = logging.getLogger(__name__)


def inbound_auth_enabled() -> bool:
    return os.environ.get("MCP_INBOUND_AUTH", "").strip().lower() == "api_key"


class APIKeyASGIMiddleware:
    def __init__(self, app, api_key: str):
        self.app = app
        self._key = api_key.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
        provided = headers.get("x-api-key", b"")
        auth = headers.get("authorization", b"")
        if not provided and auth[:7].lower() == b"bearer ":
            provided = auth[7:].strip()
        if provided and hmac.compare_digest(provided, self._key):
            return await self.app(scope, receive, send)
        body = json.dumps({"error": "unauthorized"}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="mcp"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_middleware() -> list:
    """Returns the Starlette middleware list for mcp.run(). Empty when disabled."""
    if not inbound_auth_enabled():
        logger.warning(
            "Inbound authentication is OFF: the MCP endpoint accepts unauthenticated requests."
        )
        return []
    api_key = os.environ.get("MCP_API_KEY", "")
    if len(api_key) < 16:
        raise RuntimeError(
            "MCP_INBOUND_AUTH=api_key but MCP_API_KEY is missing or shorter than 16 characters."
        )
    from starlette.middleware import Middleware

    logger.info("Inbound API-key authentication is ON.")
    return [Middleware(APIKeyASGIMiddleware, api_key=api_key)]
