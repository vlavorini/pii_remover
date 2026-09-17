#!/usr/bin/env bash
# Run the web UI locally.
#
#   ./run.sh                 -> uses .env (real model calls)
#   MOCK_MODE=true ./run.sh  -> offline, deterministic, no model calls
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [ ! -f .env ]; then
  echo "no .env found - copying .env.example (edit it before real use)" >&2
  cp .env.example .env
fi

export PORT

echo "pii-remover -> http://127.0.0.1:${PORT}"
exec uvicorn app.main:app \
  --host 127.0.0.1 \
  --port "${PORT}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  ${RELOAD:+--reload}
