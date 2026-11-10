---
name: New top-level package needs uv sync
description: Why a new *_mcp package folder is "No module named" in the running server and how to fix it
---

Rule: after adding a new top-level package (e.g. a new `xyz_mcp/` tool module), add it to `[tool.setuptools.packages.find] include` in pyproject.toml AND run `uv sync`, then restart the workflow.

**Why:** the project is installed as a setuptools *editable* package whose finder has a fixed package→path map. Tests run from the repo root still import the new folder (cwd on sys.path), but the `google-ads-mcp` entry point does not, so server.py logs "tools not loaded (missing dependencies): No module named ..." and the tools silently disappear.

**How to apply:** after `uv sync`, check the server log for "<Module> tools registered successfully" and that `list_tools` over HTTP shows the new tools.
