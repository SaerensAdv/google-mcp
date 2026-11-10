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
- `keyword_ideas` / `keyword_historical_metrics` — Google Ads Keyword Planner (`ads_mcp/keyword_planner.py`), read-only (only the "generate" calls, nothing is saved in the account). Defaults: Dutch (`1010`) + Belgium (`2056`), Google Search network. Language/locations accept IDs or names ("nl", "Nederlands", "Gent", "België"). Bids returned in account currency; ideas sorted by volume, max 200; historical max 100 keywords with 12-month trend. Keyword Planner has strict rate limits (rate-limit errors get a "wait a minute" hint).

## Browser screenshot tool (`browser_screenshot`) — OFF by default

Code in `browser_mcp/` (headless Nix Chromium via Playwright). Params: `url` (public https), `device` (`desktop` 1440×900 / `mobile` 390×844 emulation), `full_page` (default true). Returns MCP image content (JPEG) + JSON metadata.

- Every request (navigation, redirects, subresources, websockets) goes through a local CONNECT-only guard proxy that resolves DNS itself and only connects to public IPs on port 443. Chromium's own DNS is disabled.
- One capture at a time, 60s total cap, fresh browser per capture, always closed.
- Chromium launch timeout 45s (`BROWSER_LAUNCH_TIMEOUT_MS`); launch failures are logged and classified (timeout / sandbox / other).
- Pages taller than 8000px are downscaled (not cropped) up to 20000px; taller → labelled viewport fallback.

Temporary download link (compatibility test for ClickUp, `browser_mcp/linkstore.py`):
- Adds `screenshot_url` + `expires_at` to the metadata, next to the unchanged MCP image content. Route `GET /browser-screenshots/<192-bit token>`, max 10 min, cap 20 images / 50 MB, private temp dir, tokens redacted from access logs.
- Only when `BROWSER_SCREENSHOT_BASE_URL` (bare https origin; set per environment) is set and the host is in `BROWSER_SCREENSHOT_LINK_HOSTS` (default `abonnement.website`).
- Relies on a single server process (VM, one worker): the index lives in memory.

ClickUp upload (`browser_screenshot_to_clickup`, `browser_mcp/clickup_upload.py`) — OFF unless fully configured:
- Uploads the JPEG as an attachment to an EXISTING ClickUp task (API v2 `POST /task/{id}/attachment`). Never creates tasks/comments.
- Registered only with `CLICKUP_UPLOAD_ENABLED=true` + secrets `CLICKUP_API_TOKEN`, `SCREENSHOT_UPLOAD_KEY` + env `CLICKUP_UPLOAD_ALLOWED_LIST_IDS` (comma-separated).
- Every call needs header `X-Screenshot-Upload-Key` (checked per HTTP request, before capture). Task Home List is read from ClickUp and must be allowlisted.
- Duplicate protection via `request_id`: on-disk registry (`~/.cache/screenshot-upload/registry.json`, max 2000 / 180 days) + tag in attachment filename.
- Disable only this tool: set `CLICKUP_UPLOAD_ENABLED=false` (or remove it) and restart/republish.

Activation (env vars):
- `BROWSER_TOOL_ENABLED=true` **and** either `MCP_INBOUND_AUTH=api_key` (recommended) or `BROWSER_TOOL_ALLOW_UNAUTHENTICATED=true`.
- Rollback: remove `BROWSER_TOOL_ENABLED` (tool is not registered at all).

## Inbound authentication — ON (`MCP_INBOUND_AUTH=api_key`, shared env)

`ads_mcp/inbound_auth.py`. Clients must send `Authorization: Bearer <MCP_API_KEY>` or `X-API-Key: <MCP_API_KEY>` on every request (also `/browser-screenshots/*`). The ClickUp connection sends both this header and `X-Screenshot-Upload-Key`. Rollback: remove `MCP_INBOUND_AUTH` and republish (server is then open to anyone with the URL).

## Tool configuration

Tool namespaces can be toggled in `ads_mcp/tools_config.yaml` or via the `GOOGLE_ADS_MCP_TOOLS_CONFIG` env var.

## User preferences
