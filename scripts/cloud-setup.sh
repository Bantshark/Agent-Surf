#!/usr/bin/env bash
# Paste into the cloud environment's "Setup script" field.
# Network access must be Custom, with the default package-manager list included,
# plus cdn.playwright.dev and playwright.download.prss.microsoft.com
set -euo pipefail

# Diagnostics first, so a failure says which part of the network is blocked
python3 --version
uname -m
curl -sS -o /dev/null -w "pypi.org: %{http_code}\n" https://pypi.org/simple/playwright/ || echo "pypi.org: UNREACHABLE"
curl -sS -o /dev/null -w "cdn.playwright.dev: %{http_code}\n" https://cdn.playwright.dev/ || echo "cdn.playwright.dev: UNREACHABLE"

python3 -m pip install --no-cache-dir playwright==1.62.0 anthropic==1.3.0 yt-dlp==2026.8.19
python3 -m playwright install --with-deps chromium
