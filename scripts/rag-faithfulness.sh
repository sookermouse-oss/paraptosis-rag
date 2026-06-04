#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run claim-level faithfulness checks for a GPT-only vs RAG benchmark report.

Default:
  ./scripts/rag-faithfulness.sh

Default command:
  HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1 .venv/bin/python src/check_rag_faithfulness.py \
    data/eval_en_gpt_vs_rag.md \
    --nodes data/nodes.jsonl \
    --prefer-markdown \
    --out data/eval_en_faithfulness.json \
    --markdown-out data/eval_en_faithfulness.md

Common examples:
  # Deterministic only: no OpenAI API call.
  ./scripts/rag-faithfulness.sh

  # Run on the Chinese GPT-only vs RAG report.
  ./scripts/rag-faithfulness.sh \
    data/eval_zh_gpt_vs_rag.md \
    --out data/eval_zh_faithfulness.json \
    --markdown-out data/eval_zh_faithfulness.md

  # Run only Q13.
  ./scripts/rag-faithfulness.sh \
    data/eval_en_gpt_vs_rag.md \
    --start-question 13 \
    --end-question 13

  # Enable LLM judge against retrieved chunk text. This calls OpenAI.
  ./scripts/rag-faithfulness.sh \
    data/eval_en_gpt_vs_rag.md \
    --judge \
    --start-question 1 \
    --end-question 5

Supported Python options:
  eval_path                    Eval markdown or checkpoint JSONL.
  --nodes PATH                 Nodes JSONL path. Default: data/nodes.jsonl
  --storage-dir PATH           LlamaIndex storage path. Default: data/llamaindex_storage
  --embedding-model NAME       Embedding model. Default: BAAI/bge-small-en-v1.5
  --top-k N                    Reconstructed retrieved evidence count. Default: 8
  --exclude-section-type TYPE  Exclude section type. Default: methods. Repeatable.
  --include-content-type TYPE  Include content type, e.g. abstract or fulltext. Repeatable.
  --exclude-content-type TYPE  Exclude content type, e.g. abstract. Repeatable.
  --max-context-chars N        Max chars per chunk for LLM judge. Default: 1600
  --judge                      Enable LLM judge. Disabled by default.
  --model NAME                 LLM judge model. Default: gpt-5-mini
  --start-question N           Only run questions with number >= N.
  --end-question N             Only run questions with number <= N.
  --prefer-markdown            Parse markdown even if a checkpoint exists.
  --out PATH                   JSON output path. Default: data/faithfulness.json
  --markdown-out PATH          Markdown output path. Default: data/faithfulness.md

Notes:
  - Judge mode is OFF by default.
  - Without --judge, this does not call OpenAI.
  - With --judge, OPENAI_API_KEY is required.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ ! -f "src/check_rag_faithfulness.py" || ! -f "data/nodes.jsonl" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

needs_openai=0
for arg in "$@"; do
  if [[ "$arg" == "--judge" ]]; then
    needs_openai=1
    break
  fi
done

if [[ "$needs_openai" -eq 1 ]]; then
  if [[ -z "${OPENAI_API_KEY:-}" && -f "$HOME/.zshrc" ]]; then
    # shellcheck disable=SC1090
    source "$HOME/.zshrc"
  fi

  if [[ -z "${OPENAI_API_KEY:-}" && -f "$HOME/.env" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$HOME/.env"
    set +a
  fi

  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "Error: OPENAI_API_KEY is not set, but --judge was requested." >&2
    exit 1
  fi
fi

if [[ $# -eq 0 ]]; then
  set -- \
    data/eval_en_gpt_vs_rag.md \
    --nodes data/nodes.jsonl \
    --prefer-markdown \
    --out data/eval_en_faithfulness.json \
    --markdown-out data/eval_en_faithfulness.md
fi

HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 \
  .venv/bin/python src/check_rag_faithfulness.py "$@"
