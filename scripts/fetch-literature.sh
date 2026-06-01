#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "src/literature_discovery.py" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

.venv/bin/python -m src.literature_discovery "$@"
