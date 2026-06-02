#!/usr/bin/env bash
set -euo pipefail

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

HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 \
  .venv/bin/python src/benchmark_zh_rag_vs_gpt.py \
  --questions data/eval_zh_questions.txt \
  --output data/eval_zh_gpt_vs_rag.md
