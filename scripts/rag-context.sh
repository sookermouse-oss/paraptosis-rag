#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "src/rag_answer.py" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "Usage: ./scripts/rag-context.sh \"What is the role of PI4KB in paraptosis?\"" >&2
  exit 1
fi

HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 .venv/bin/python src/rag_answer.py \
  --query "$1" \
  --show-context \
  --save-context
