#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "src/enrich_xml.py" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

.venv/bin/python -m src.enrich_xml "$@"
