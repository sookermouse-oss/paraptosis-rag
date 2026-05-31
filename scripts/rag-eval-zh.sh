#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "src/rag_answer.py" || ! -f "evaluation/questions_zh.txt" ]]; then
  echo "Error: run this script from the project root." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Error: .venv/bin/python not found or not executable." >&2
  exit 1
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Error: OPENAI_API_KEY is not set." >&2
  exit 1
fi

output="data/eval_zh_answers.md"
mkdir -p data

{
  echo "# 中文 RAG Evaluation"
  echo
} > "$output"

declare -a questions=()
declare -a normalized_queries=()
declare -a result_files=()
question_index=0
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

while IFS= read -r question; do
  [[ -z "${question//[[:space:]]/}" ]] && continue
  question_index=$((question_index + 1))
  questions+=("$question")

  result_file="${tmp_dir}/q${question_index}.txt"
  HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 .venv/bin/python src/rag_answer.py --query "$question" > "$result_file"

  normalized_query="$(sed -n 's/^Normalized retrieval query: //p' "$result_file" | head -n 1)"
  normalized_queries+=("$normalized_query")
  result_files+=("$result_file")
done < evaluation/questions_zh.txt

{
  echo "## Query Table"
  echo
  echo "| # | Original query | Normalized retrieval query |"
  echo "| --- | --- | --- |"
  for i in "${!questions[@]}"; do
    row=$((i + 1))
    question="${questions[$i]}"
    normalized_query="${normalized_queries[$i]}"
    printf '| %s | %s | %s |\n' "$row" "$question" "$normalized_query"
  done
  echo
} >> "$output"

for i in "${!questions[@]}"; do
  row=$((i + 1))
  question="${questions[$i]}"
  result_file="${result_files[$i]}"

  {
    echo "## Q${row}: ${question}"
    echo
    cat "$result_file"
    echo
    echo "---"
    echo
  } >> "$output"
done
