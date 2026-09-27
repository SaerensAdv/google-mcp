---
name: Browser screenshot tool decisions
description: Why browser_screenshot is gated, how Chromium runs here, and capture-tuning lessons
---

- Uses Nix `chromium` + pip `playwright` with `executable_path` (no Playwright browser download, no `playwright-driver` nix pkg). Chromium sandbox works in the dev container; production availability not yet verified — capture fails explicitly rather than silently going unsandboxed.
  **Why:** smallest dependency set that survives uv/nix deployment.
- The live MCP endpoint has NO inbound auth on purpose (removed earlier for ClickUp compatibility). The user explicitly required: don't change live auth without their approval; the browser tool stays off unless auth is on or an explicit override env var is set.
  **How to apply:** never flip MCP_INBOUND_AUTH or BROWSER_TOOL_ENABLED yourself.
- Scroll-reveal sites (e.g. abonnement.website) render blank sections in full-page captures unless scrolling is slow (~400ms per 0.8 viewport) and starts after network idle (React content renders after `load`).
- When testing process cleanup, `pgrep -f <pattern>` also matches the calling shell if the pattern appears in the command/heredoc — match the exact Chromium binary path.
