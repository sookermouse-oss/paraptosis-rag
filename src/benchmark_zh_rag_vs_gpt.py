from __future__ import annotations

import argparse
import datetime as dt
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


def load_questions(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_group = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            current_group = stripped.lstrip("#").strip()
            continue
        match = QUESTION_RE.match(stripped)
        if not match:
            continue
        rows.append(
            {
                "number": match.group(1),
                "question": match.group(2),
                "group": current_group,
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
) -> list[dict[str, Any]]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set.")

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(questions, start=1):
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
    return rows


def write_markdown(rows: list[dict[str, Any]], output_path: Path, questions_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out:
        out.write("# GPT-only vs RAG 中文对照实验\n\n")
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
    parser = argparse.ArgumentParser(description="Run Chinese GPT-only vs RAG benchmark without scoring.")
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
    args = parser.parse_args()

    questions = load_questions(args.questions)
    rows = run_comparison(
        questions=questions,
        nodes_path=args.nodes,
        storage_dir=args.storage_dir,
        embedding_model=args.embedding_model,
        openai_model=args.openai_model,
        top_k=args.top_k,
        exclude_section_types=args.exclude_section_type,
        max_context_chars=args.max_context_chars,
    )
    write_markdown(rows, args.output, args.questions)
    print(f"Wrote {args.output}", flush=True)


if __name__ == "__main__":
    main()
