from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from benchmark_rag_vs_gpt import call_gpt_only
from hybrid_retrieval import (
    DEFAULT_BM25_ONLY_PENALTY,
    DEFAULT_BM25_WEIGHT,
    DEFAULT_EMBEDDING_ONLY_PENALTY,
    DEFAULT_EMBEDDING_WEIGHT,
    DEFAULT_MODEL,
    DEFAULT_NODES_PATH,
    DEFAULT_STORAGE_DIR,
)
from rag_answer import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_TOP_K,
    MAX_CONTEXT_CHARS_PER_NODE,
    retrieval_quality,
    run_rag_pipeline,
)


DEFAULT_QUESTIONS_PATH = Path("data/eval_zh_questions.txt")
DEFAULT_OUTPUT_PATH = Path("data/eval_zh_gpt_vs_rag.md")
DEFAULT_EXCLUDED_SECTION_TYPES = ["methods"]
QUESTION_RE = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*$")
HEADING_RE = re.compile(r"^(#+)\s*(.+?)\s*$")


def load_questions(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_group = ""
    current_topic = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading_match = HEADING_RE.match(stripped)
        if heading_match:
            level = len(heading_match.group(1))
            heading = heading_match.group(2)
            if level == 1:
                current_group = heading
                current_topic = ""
            else:
                current_topic = heading
            continue
        match = QUESTION_RE.match(stripped)
        if match:
            number = match.group(1)
            question = match.group(2)
        elif stripped.endswith("?") or stripped.endswith("？"):
            number = str(len(rows) + 1)
            question = stripped
        else:
            continue
        group = current_group
        if current_topic:
            group = f"{current_group} / {current_topic}" if current_group else current_topic
        rows.append(
            {
                "number": number,
                "question": question,
                "group": group,
            }
        )
    return rows


def run_comparison(
    questions: list[dict[str, str]],
    nodes_path: Path,
    storage_dir: Path,
    embedding_model: str,
    openai_model: str,
    top_k: int,
    exclude_section_types: list[str],
    max_context_chars: int,
    questions_path: Path,
    output_path: Path,
    checkpoint_path: Path,
    resume: bool,
) -> list[dict[str, Any]]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set.")

    rows = load_checkpoint(checkpoint_path) if resume else []
    completed_numbers = {row["number"] for row in rows}
    if rows:
        write_markdown(rows, output_path, questions_path)
        print(f"Loaded {len(rows)} checkpointed results from {checkpoint_path}", flush=True)

    for index, item in enumerate(questions, start=1):
        if item["number"] in completed_numbers:
            print(f"Skipping Q{item['number']} ({index}/{len(questions)}): already checkpointed", flush=True)
            continue
        question = item["question"]
        print(f"Running Q{item['number']} ({index}/{len(questions)}): {question}", flush=True)

        gpt_answer = call_gpt_only(openai_model, question)
        rag_result = run_rag_pipeline(
            query=question,
            nodes_path=nodes_path,
            storage_dir=storage_dir,
            embedding_model=embedding_model,
            openai_model=openai_model,
            top_k=top_k,
            exclude_section_types=exclude_section_types,
            include_content_types=[],
            exclude_content_types=[],
            bm25_weight=DEFAULT_BM25_WEIGHT,
            embedding_weight=DEFAULT_EMBEDDING_WEIGHT,
            penalize_single_source=True,
            bm25_only_penalty=DEFAULT_BM25_ONLY_PENALTY,
            embedding_only_penalty=DEFAULT_EMBEDDING_ONLY_PENALTY,
            max_context_chars=max_context_chars,
            no_query_normalization=False,
            show_context=False,
            save_context=False,
            debug_context_path=Path("data/debug_context.txt"),
        )
        quality = retrieval_quality(rag_result["evidence"], rag_result["normalized_query"])
        rows.append(
            {
                **item,
                "normalized_query": rag_result["normalized_query"],
                "retrieval_strength": quality["retrieval_strength"],
                "evidence_diversity": quality["evidence_diversity"],
                "top1_score": quality["top1_score"],
                "top5_avg_score": quality["top5_avg_score"],
                "coverage": quality["query_coverage"],
                "evidence_count": len(rag_result["evidence"]),
                "gpt_answer": gpt_answer,
                "rag_answer": rag_result["answer"],
            }
        )
        append_checkpoint(checkpoint_path, rows[-1])
        write_markdown(rows, output_path, questions_path)
        print(f"Checkpointed Q{item['number']} to {checkpoint_path}", flush=True)
    return rows


def load_checkpoint(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen_numbers: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        number = str(row.get("number", ""))
        if number and number not in seen_numbers:
            rows.append(row)
            seen_numbers.add(number)
    return rows


def append_checkpoint(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False))
        out.write("\n")


def checkpoint_path_for_output(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".checkpoint.jsonl")


def filter_question_range(
    questions: list[dict[str, str]],
    start_question: int | None,
    end_question: int | None,
) -> list[dict[str, str]]:
    if start_question is None and end_question is None:
        return questions
    filtered: list[dict[str, str]] = []
    for item in questions:
        try:
            number = int(item["number"])
        except (TypeError, ValueError):
            continue
        if start_question is not None and number < start_question:
            continue
        if end_question is not None and number > end_question:
            continue
        filtered.append(item)
    return filtered


def write_markdown(rows: list[dict[str, Any]], output_path: Path, questions_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out:
        out.write("# GPT-only vs RAG Benchmark\n\n")
        out.write(f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}\n\n")
        out.write(f"Questions file: `{questions_path}`\n\n")
        out.write("No automatic correctness scoring is applied.\n\n")
        out.write("## Summary\n\n")
        out.write(f"- Total questions: {len(rows)}\n")
        out.write(f"- HIGH retrieval strength: {sum(1 for row in rows if row['retrieval_strength'] == 'HIGH')}\n")
        out.write(f"- MEDIUM retrieval strength: {sum(1 for row in rows if row['retrieval_strength'] == 'MEDIUM')}\n")
        out.write(f"- LOW retrieval strength: {sum(1 for row in rows if row['retrieval_strength'] == 'LOW')}\n")
        out.write(f"- HIGH evidence diversity: {sum(1 for row in rows if row['evidence_diversity'] == 'HIGH')}\n")
        out.write(f"- MEDIUM evidence diversity: {sum(1 for row in rows if row['evidence_diversity'] == 'MEDIUM')}\n")
        out.write(f"- LOW evidence diversity: {sum(1 for row in rows if row['evidence_diversity'] == 'LOW')}\n\n")

        out.write("## Results\n\n")
        for row in rows:
            out.write(f"## Q{row['number']}: {row['question']}\n\n")
            out.write(f"- Expected group: {row['group']}\n")
            out.write(f"- Retrieval strength: {row['retrieval_strength']}\n")
            out.write(f"- Evidence diversity: {row['evidence_diversity']}\n")
            out.write(f"- Top1 score: {row['top1_score']:.4f}\n")
            out.write(f"- Top5 avg score: {row['top5_avg_score']:.4f}\n")
            out.write(f"- Coverage: {row['coverage']:.4f}\n")
            out.write(f"- Evidence count: {row['evidence_count']}\n")
            out.write(f"- Normalized retrieval query: {row['normalized_query']}\n\n")
            out.write("### GPT answer\n\n")
            out.write(row["gpt_answer"].strip())
            out.write("\n\n### RAG answer\n\n")
            out.write(row["rag_answer"].strip())
            out.write("\n\n---\n\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GPT-only vs RAG benchmark without scoring.")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_PATH)
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE_DIR)
    parser.add_argument("--embedding-model", default=DEFAULT_MODEL)
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument(
        "--exclude-section-type",
        action="append",
        default=DEFAULT_EXCLUDED_SECTION_TYPES.copy(),
        help="Section type to exclude. Defaults to methods. Can be passed multiple times.",
    )
    parser.add_argument("--max-context-chars", type=int, default=MAX_CONTEXT_CHARS_PER_NODE)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the checkpoint file and skip completed questions.",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="Checkpoint JSONL path. Default: <output>.checkpoint.jsonl",
    )
    parser.add_argument("--start-question", type=int, default=None, help="Only run questions with number >= this value.")
    parser.add_argument("--end-question", type=int, default=None, help="Only run questions with number <= this value.")
    args = parser.parse_args()

    questions = filter_question_range(
        load_questions(args.questions),
        args.start_question,
        args.end_question,
    )
    checkpoint_path = args.checkpoint or checkpoint_path_for_output(args.output)
    if not args.resume and checkpoint_path.exists():
        checkpoint_path.unlink()
    rows = run_comparison(
        questions=questions,
        nodes_path=args.nodes,
        storage_dir=args.storage_dir,
        embedding_model=args.embedding_model,
        openai_model=args.openai_model,
        top_k=args.top_k,
        exclude_section_types=args.exclude_section_type,
        max_context_chars=args.max_context_chars,
        questions_path=args.questions,
        output_path=args.output,
        checkpoint_path=checkpoint_path,
        resume=args.resume,
    )
    write_markdown(rows, args.output, args.questions)
    print(f"Wrote {args.output}", flush=True)
    print(f"Checkpoint: {checkpoint_path}", flush=True)


if __name__ == "__main__":
    main()
