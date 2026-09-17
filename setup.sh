#!/usr/bin/env bash
# One-shot local bootstrap: venv, dependencies, git hook, sample files, tests.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV=".venv"

echo "==> python: $($PYTHON_BIN --version)"

if [ ! -d "$VENV" ]; then
  echo "==> creating virtualenv"
  "$PYTHON_BIN" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements-dev.txt

if [ ! -f .env ]; then
  echo "==> creating .env from .env.example"
  cp .env.example .env
  KEY="$(python -m app.core.crypto)"
  # portability: works with GNU sed and BSD sed
  python - "$KEY" <<'PY'
import pathlib, sys
key = sys.argv[1]
path = pathlib.Path(".env")
text = path.read_text()
text = text.replace("ENCRYPTION_KEY=\n", f"ENCRYPTION_KEY={key}\n", 1)
path.write_text(text)
print("    wrote ENCRYPTION_KEY (generated) to .env")
PY
  if ! grep -q '^OPENAI_API_KEY=.\+' .env; then
    echo "    NOTE: set OPENAI_BASE_URL / OPENAI_API_KEY / VISION_MODEL / TEXT_MODEL in .env"
    echo "          until then the app runs, but model calls fail."
  fi
fi

echo "==> generating sample documents"
python scripts/make_samples.py

echo "==> running the test suite (offline, MOCK_MODE)"
MOCK_MODE=true pytest -q

cat <<'EOF'

==> ready

  source .venv/bin/activate
  uvicorn app.main:app --reload --port 8000     # http://127.0.0.1:8000
  ./run.sh                                       # same, without the venv dance
  MOCK_MODE=true ./run.sh                        # fully offline demo
EOF
