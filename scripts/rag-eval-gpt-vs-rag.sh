#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run GPT-only vs RAG benchmark and write a markdown report.

Default:
  ./scripts/rag-eval-gpt-vs-rag.sh

Default command:
  HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1 .venv/bin/python src/benchmark_zh_rag_vs_gpt.py \
    --questions data/eval_zh_questions.txt \
    --output data/eval_zh_gpt_vs_rag.md

Common examples:
  ./scripts/rag-eval-gpt-vs-rag.sh \
    --questions data/eval_en_questions.txt \
    --output data/eval_en_gpt_vs_rag.md

  ./scripts/rag-eval-gpt-vs-rag.sh \
    --questions data/eval_zh_questions.txt \
    --output data/eval_zh_gpt_vs_rag.md \
    --top-k 8

  # Resume after a stalled or interrupted long run.
  ./scripts/rag-eval-gpt-vs-rag.sh \
    --questions data/eval_zh_questions.txt \
    --output data/eval_zh_gpt_vs_rag.md \
    --resume

  # Run only one question, useful for retrying a stuck item.
  ./scripts/rag-eval-gpt-vs-rag.sh \
    --questions data/eval_zh_questions.txt \
    --output data/eval_zh_q28_check.md \
    --start-question 28 \
    --end-question 28

Supported Python options:
  --questions PATH             Questions file. Default: data/eval_zh_questions.txt
  --output PATH                Markdown report path. Default: data/eval_zh_gpt_vs_rag.md
  --nodes PATH                 Nodes JSONL path. Default: data/nodes.jsonl
  --storage-dir PATH           LlamaIndex storage path. Default: data/llamaindex_storage
  --embedding-model NAME       Embedding model. Default: BAAI/bge-small-en-v1.5
  --openai-model NAME          OpenAI model. Default: gpt-5-mini with medium reasoning effort
  --top-k N                    Retrieved evidence count. Default: 8
  --exclude-section-type TYPE  Exclude section type. Default: methods. Repeatable.
  --max-context-chars N        Max context chars per node. Default: 1600
  --resume                     Continue from <output>.checkpoint.jsonl.
  --checkpoint PATH            Checkpoint path. Default: <output>.checkpoint.jsonl
  --start-question N           Only run questions with number >= N.
  --end-question N             Only run questions with number <= N.

Notes:
  - Questions can be Chinese or English.
  - OPENAI_API_KEY is required for actual benchmark runs, but not for --help.
  - A checkpoint is written after each completed question, so interrupted runs can resume.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ ! -f "src/benchmark_zh_rag_vs_gpt.py" || ! -f "data/eval_zh_questions.txt" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

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
  echo "Error: OPENAI_API_KEY is not set." >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  set -- \
    --questions data/eval_zh_questions.txt \
    --output data/eval_zh_gpt_vs_rag.md
fi

HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 \
  .venv/bin/python src/benchmark_zh_rag_vs_gpt.py "$@"
