#!/usr/bin/env bash
set -euo pipefail

port="${SHIELD_PORT:-1026}"

exec /opt/cnc/.venv/bin/uvicorn app.shield:app --host 0.0.0.0 --port "$port"
