#!/usr/bin/env bash
# Hostinger Ubuntu VPS — one-time / fresh machine bootstrap for this repo.
# Run from repo root AFTER clone:   chmod +x setup.sh && ./setup.sh
# Do not commit secrets; copy .env and config/accounts.json on the server separately.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "This script must be run as root (e.g. sudo ./setup.sh) for apt and global npm."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> apt update && apt upgrade -y"
apt-get update -y
apt-get upgrade -y

echo "==> Installing system packages"
apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  git \
  curl \
  ca-certificates \
  build-essential \
  nodejs \
  npm

echo "==> Installing PM2 globally"
npm install -g pm2

echo "==> Creating Python virtual environment at ${SCRIPT_DIR}/venv"
python3 -m venv "${SCRIPT_DIR}/venv"

echo "==> Upgrading pip and installing requirements.txt"
"${SCRIPT_DIR}/venv/bin/python" -m pip install --upgrade pip
"${SCRIPT_DIR}/venv/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"

echo "==> Done. Next steps (manual on server):"
echo "    1. Copy .env and config/accounts.json (or use your secrets manager)."
echo "    2. pm2 start ecosystem.config.js"
echo "    3. pm2 save && pm2 startup   # optional: survive reboot"
