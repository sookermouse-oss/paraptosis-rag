#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "src/hybrid_retrieval.py" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 .venv/bin/python src/hybrid_retrieval.py \
  --top-k 10 \
  --exclude-section-type methods
