#!/usr/bin/env bash
# Cron-friendly entrypoint: loads .env via Python (dotenv in config).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
exec python3 main.py --daily-automation "$@"
