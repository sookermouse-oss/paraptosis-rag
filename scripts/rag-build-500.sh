#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "src/build_nodes.py" || ! -f "src/llamaindex_retrieval.py" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

python3 src/build_nodes.py --limit 500
rm -rf data/llamaindex_storage
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 .venv/bin/python src/llamaindex_retrieval.py build
