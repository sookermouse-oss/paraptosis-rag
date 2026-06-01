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

XML_DIR="${XML_DIR:-fulltext_xml}"
LEGACY_XML_DIR="/Users/shuangsu/Documents/Projects/paraptosis-biorxiv-job/fulltext_xml"

if [[ ! -d "$XML_DIR" && -d "$LEGACY_XML_DIR" ]]; then
  XML_DIR="$LEGACY_XML_DIR"
fi

python3 src/build_nodes.py --xml-dir "$XML_DIR" --limit 500
rm -rf data/llamaindex_storage
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 .venv/bin/python src/llamaindex_retrieval.py build
