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

## Production cold start (2026-09-27)
- Published VM starts slowly (~34s server boot; first Chromium launch can be far slower than dev's 0.3–7s). A real "no sandbox" failure aborts in ~0.1s with a FATAL setuid_sandbox_host line; a ~30s failure means launch timeout, not sandbox.
- **How to apply:** diagnose from the "Chromium launch failed (kind=...)" log line in deployment logs before touching sandbox settings.
- Confirmed in prod: the first capture after a VM (re)start can hit the 45s launch timeout; the second call succeeds. A retry with the same request_id is safe (capture_failed does not block it). A new instance starting while the old one is still serving can cut off an in-flight call: check the task's attachments before retrying. No-userns can be simulated in dev: `unshare -Ur sh -c 'echo 1 > /proc/sys/user/max_user_namespaces; exec unshare -U --map-user=1000 --map-group=1000 <cmd>'`.

- Test servers on a second port: start with ShellExec run_in_background; `nohup ... &` inside a normal ShellExec dies when that shell exits.
- ClickUp upload tool only accepts regular task IDs; custom IDs (e.g. SA3-8) must be resolved via GET /task/<id>?custom_task_ids=true&team_id=<workspace>. **Why:** kept scope minimal at user's request.
