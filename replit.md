# Google Ads MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that exposes Google Ads API functionality as tools for AI agents.

## Stack

- **Language:** Python 3.10
- **Framework:** FastMCP 3.4.4
- **API:** Google Ads API (via `google-ads` client library)

## How to run

The workflow `Start application` runs `google-ads-mcp` and listens on port 8080 in HTTP/streamable-http mode.

### Required secrets

| Secret | Description |
|--------|-------------|
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` | Google OAuth 2.0 client ID (Web application type) |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` | Google OAuth 2.0 client secret |

### Environment variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_ADS_MCP_BASE_URL` | Public base URL of this server (set to the Replit dev domain) |
| `PORT` | Port to listen on (default: 8080) |

### MCP endpoint

```
https://<replit-dev-domain>/mcp
```

## Tools available

- `search` — Query Google Ads account data via GAQL
- `get_resource_metadata` — Get metadata for a Google Ads resource type
- `list_accessible_customers` — List customer IDs accessible to the authenticated user

## Browser screenshot tool (`browser_screenshot`) — OFF by default

Code in `browser_mcp/` (headless Nix Chromium via Playwright). Params: `url` (public https), `device` (`desktop` 1440×900 / `mobile` 390×844 emulation), `full_page` (default true). Returns MCP image content (JPEG) + JSON metadata.

- Every request (navigation, redirects, subresources, websockets) goes through a local CONNECT-only guard proxy that resolves DNS itself and only connects to public IPs on port 443. Chromium's own DNS is disabled.
- One capture at a time, 60s total cap, fresh browser per capture, always closed.
- Pages taller than 8000px are downscaled (not cropped) up to 20000px; taller → labelled viewport fallback.

Activation (env vars):
- `BROWSER_TOOL_ENABLED=true` **and** either `MCP_INBOUND_AUTH=api_key` (recommended) or `BROWSER_TOOL_ALLOW_UNAUTHENTICATED=true`.
- Rollback: remove `BROWSER_TOOL_ENABLED` (tool is not registered at all).

## Inbound authentication — OFF by default

`ads_mcp/inbound_auth.py`. When `MCP_INBOUND_AUTH=api_key`, clients must send `Authorization: Bearer <MCP_API_KEY>` or `X-API-Key: <MCP_API_KEY>`. Unset = current open behaviour (was removed earlier for ClickUp compatibility).

## Tool configuration

Tool namespaces can be toggled in `ads_mcp/tools_config.yaml` or via the `GOOGLE_ADS_MCP_TOOLS_CONFIG` env var.

## User preferences
