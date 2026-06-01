#!/usr/bin/env bash
set -euo pipefail

questions_file="data/eval_zh_questions.txt"
output_file="data/eval_zh_answers.md"

if [[ ! -f "src/rag_answer.py" || ! -f "$questions_file" ]]; then
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

mkdir -p data
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

results_tsv="${tmp_dir}/results.tsv"
printf 'number\tquestion\texpected_group\tconfidence\ttop1\ttop5\tcoverage\tevidence_count\tresult_file\n' > "$results_tsv"

declare current_group=""
question_count=0
expected_high=0
expected_medium=0
expected_low=0
expected_stress=0
total_questions="$(grep -E '^[[:space:]]*[0-9]+\.' "$questions_file" | wc -l | tr -d ' ')"

while IFS= read -r line; do
  stripped="${line#"${line%%[![:space:]]*}"}"
  stripped="${stripped%"${stripped##*[![:space:]]}"}"

  [[ -z "$stripped" ]] && continue

  if [[ "$stripped" == \#* ]]; then
    current_group="${stripped#\# }"
    continue
  fi

  if [[ "$stripped" =~ ^([0-9]+)\.[[:space:]]*(.*)$ ]]; then
    number="${BASH_REMATCH[1]}"
    question="${BASH_REMATCH[2]}"
  else
    echo "Skipping unrecognized line: $line" >&2
    continue
  fi

  question_count=$((question_count + 1))
  case "$current_group" in
    HIGH*) expected_high=$((expected_high + 1)) ;;
    MEDIUM*) expected_medium=$((expected_medium + 1)) ;;
    LOW*) expected_low=$((expected_low + 1)) ;;
    "Query Rewrite Stress Test"*) expected_stress=$((expected_stress + 1)) ;;
  esac

  result_file="${tmp_dir}/q${number}.txt"
  echo "Running Q${number}/${total_questions}: ${question}" >&2
  HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" HF_HUB_DISABLE_XET=1 \
    .venv/bin/python src/rag_answer.py --query "$question" > "$result_file"

  confidence="$(sed -n 's/^Retrieval confidence: //p' "$result_file" | head -n 1)"
  top1="$(sed -n 's/^top1_score: //p' "$result_file" | head -n 1)"
  top5="$(sed -n 's/^top5_avg_score: //p' "$result_file" | head -n 1)"
  coverage="$(sed -n 's/^query_coverage: //p' "$result_file" | head -n 1)"
  evidence_count="$(grep -E '^[0-9]+\. ' "$result_file" || true)"
  evidence_count="$(printf '%s\n' "$evidence_count" | sed '/^$/d' | wc -l | tr -d ' ')"

  confidence="${confidence:-UNKNOWN}"
  top1="${top1:-0}"
  top5="${top5:-0}"
  coverage="${coverage:-0}"
  evidence_count="${evidence_count:-0}"

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$number" "$question" "$current_group" "$confidence" "$top1" "$top5" "$coverage" "$evidence_count" "$result_file" >> "$results_tsv"
done < "$questions_file"

.venv/bin/python - "$results_tsv" "$output_file" "$questions_file" \
  "$expected_high" "$expected_medium" "$expected_low" "$expected_stress" <<'PY'
import csv
import datetime as dt
import pathlib
import sys

results_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
questions_file = sys.argv[3]
expected_high = int(sys.argv[4])
expected_medium = int(sys.argv[5])
expected_low = int(sys.argv[6])
expected_stress = int(sys.argv[7])


def extract_answer(text: str) -> str:
    lines = text.splitlines()
    answer_lines = []
    in_answer = False
    for line in lines:
        if line.strip() == "Answer:":
            in_answer = True
            continue
        if line.strip() == "Evidence:":
            break
        if in_answer:
            answer_lines.append(line)
    return "\n".join(answer_lines).strip() or "(No Answer block found.)"


def mean(values):
    nums = []
    for value in values:
        try:
            nums.append(float(value))
        except (TypeError, ValueError):
            pass
    return sum(nums) / len(nums) if nums else 0.0


with results_path.open(newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f, delimiter="\t"))

confidence_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
for row in rows:
    confidence_counts[row["confidence"]] = confidence_counts.get(row["confidence"], 0) + 1

avg_top1 = mean(row["top1"] for row in rows)
avg_top5 = mean(row["top5"] for row in rows)

with output_path.open("w", encoding="utf-8") as out:
    out.write("# 中文 RAG QA Benchmark\n\n")
    out.write(f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}\n\n")
    out.write(f"Questions file: `{questions_file}`\n\n")
    out.write("## Summary\n\n")
    out.write(f"- Total questions: {len(rows)}\n")
    out.write(f"- Expected HIGH count: {expected_high}\n")
    out.write(f"- Expected MEDIUM count: {expected_medium}\n")
    out.write(f"- Expected LOW count: {expected_low}\n")
    out.write(f"- Query rewrite stress test count: {expected_stress}\n")
    out.write(f"- Confidence HIGH count: {confidence_counts.get('HIGH', 0)}\n")
    out.write(f"- Confidence MEDIUM count: {confidence_counts.get('MEDIUM', 0)}\n")
    out.write(f"- Confidence LOW count: {confidence_counts.get('LOW', 0)}\n")
    unknown = sum(v for k, v in confidence_counts.items() if k not in {"HIGH", "MEDIUM", "LOW"})
    if unknown:
        out.write(f"- Confidence UNKNOWN/other count: {unknown}\n")
    out.write(f"- Average top1 score: {avg_top1:.4f}\n")
    out.write(f"- Average top5 score: {avg_top5:.4f}\n\n")

    out.write("## Results\n\n")
    for row in rows:
        result_text = pathlib.Path(row["result_file"]).read_text(encoding="utf-8")
        answer = extract_answer(result_text)
        out.write(f"## Q{row['number']}: {row['question']}\n\n")
        out.write(f"- Expected group: {row['expected_group']}\n")
        out.write(f"- Confidence: {row['confidence']}\n")
        out.write(f"- Top1 score: {row['top1']}\n")
        out.write(f"- Top5 avg score: {row['top5']}\n")
        out.write(f"- Coverage: {row['coverage']}\n")
        out.write(f"- Evidence count: {row['evidence_count']}\n\n")
        out.write("### Answer\n\n")
        out.write(answer)
        out.write("\n\n---\n\n")
PY

echo "Wrote ${output_file}" >&2
