#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "src/rag_eval_web_server.py" || ! -f "docs/rag_eval_web.html" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 \
  .venv/bin/python src/rag_eval_web_server.py "$@"
