---
name: uv deployment venv fix
description: Why uv sync failed with "Permission denied /nix/store/..." and how the env must be built
---

Rule: `.pythonlibs` must be a real venv created against the actual nix Python binary, not Replit's `python-wrapped` wrapper, or `uv sync` computes install paths into the read-only nix store.

**Why:** Deployment builds run `uv lock` + `uv sync`. With a pip-created `.pythonlibs` (plain dir) or a venv whose `pyvenv.cfg` `home` points at the `python-wrapped` wrapper, uv installed wheels into `/nix/store/.../site-packages` → `Permission denied (os error 13)` and the publish failed.

**How to apply:** If uv install errors mention `/nix/store/...` destinations: `rm -rf .pythonlibs && uv venv --python <real nix python3.x binary> .pythonlibs && uv sync`. The project also needs a `[build-system]` in pyproject (plus `tool.uv.package = true`) for entry-point scripts like `google-ads-mcp` to be installed.
