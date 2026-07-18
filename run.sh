#!/usr/bin/env bash
# Avvio sviluppo di TradeMax.
#
#   ./run.sh            # solo backend (serve anche il frontend buildato, se esiste)
#   ./run.sh --front    # backend + dev server Vite con hot reload
#
# Prerequisiti: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
#               (cd frontend && npm install) per la modalità --front o per buildare.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv/bin"
if [ ! -x "$VENV/uvicorn" ]; then
  echo "Ambiente non inizializzato. Esegui prima:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [ "${1:-}" = "--front" ]; then
  (cd frontend && npm run dev) &
  FRONT_PID=$!
  trap 'kill "$FRONT_PID" 2>/dev/null || true' EXIT
fi

cd backend
exec "../$VENV/uvicorn" app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
