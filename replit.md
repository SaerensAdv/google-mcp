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

## Tool configuration

Tool namespaces can be toggled in `ads_mcp/tools_config.yaml` or via the `GOOGLE_ADS_MCP_TOOLS_CONFIG` env var.

## User preferences
