#!/usr/bin/env bash
# Start FastAPI on :8080 and Vite on :5173 (same as npm run dashboard:dev).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
python3 -m uvicorn dashboard_app.main:app --reload --host 127.0.0.1 --port 8080 &
sleep 1
exec npm run dev --prefix frontend
