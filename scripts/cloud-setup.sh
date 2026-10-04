#!/usr/bin/env bash
# Paste into the cloud environment's "Setup script" field.
# Network access must be Custom: defaults + cdn.playwright.dev + playwright.download.prss.microsoft.com
set -euo pipefail

pip install --no-cache-dir playwright==1.62.0 anthropic==1.3.0 yt-dlp==2026.8.19
python -m playwright install --with-deps chromium
