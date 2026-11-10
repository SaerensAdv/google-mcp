#!/bin/bash
# Runs automatically after a task merge: install/refresh Python deps and the
# project itself (new top-level tool packages are only picked up after uv sync).
set -e
uv sync --frozen 2>&1 || uv sync
