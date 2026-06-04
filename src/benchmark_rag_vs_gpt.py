from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from hybrid_retrieval import (
    DEFAULT_BM25_ONLY_PENALTY,
    DEFAULT_BM25_WEIGHT,
    DEFAULT_EMBEDDING_ONLY_PENALTY,
    DEFAULT_EMBEDDING_WEIGHT,
    DEFAULT_MODEL,
    DEFAULT_NODES_PATH,
    DEFAULT_STORAGE_DIR,
    hybrid_search,
    load_index,
)
from rag_answer import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_TIMEOUT_SECONDS,
    DEFAULT_TOP_K,
    MAX_CONTEXT_CHARS_PER_NODE,
    call_openai,
    enrich_evidence,
    openai_reasoning_kwargs,
)
from search_nodes import filter_nodes, load_nodes


DEFAULT_QUERIES = [
    "How does ER stress induce paraptosis?",
    "What is the role of PI4KB in paraptosis?",
    "Can paraptosis help overcome drug resistance?",
    "How does calcium homeostasis contribute to paraptosis?",
]
DEFAULT_EXCLUDED_SECTION_TYPES = ["methods"]

GPT_ONLY_INSTRUCTIONS = """Answer the biomedical question directly.

Rules:
- Do not use retrieved context.
- If you are uncertain, say so.
- Do not invent citations.
- Keep the answer concise and mechanistic.
"""


def run_benchmark(
    queries: list[str],
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

    all_nodes = load_nodes(nodes_path)
    filtered_nodes = filter_nodes(all_nodes, exclude_section_types)
    node_lookup = {node["node_id"]: node for node in all_nodes}
    index = load_index(storage_dir, embedding_model)

    rows: list[dict[str, Any]] = []
    for query in queries:
        gpt_only_answer = call_gpt_only(openai_model, query)
        _, _, retrieval_results = hybrid_search(
            nodes=filtered_nodes,
            index=index,
            query=query,
            top_k=top_k,
            exclude_section_types=exclude_section_types,
            bm25_weight=DEFAULT_BM25_WEIGHT,
            embedding_weight=DEFAULT_EMBEDDING_WEIGHT,
            penalize_single_source=True,
            bm25_only_penalty=DEFAULT_BM25_ONLY_PENALTY,
            embedding_only_penalty=DEFAULT_EMBEDDING_ONLY_PENALTY,
        )
        evidence = enrich_evidence(retrieval_results, node_lookup, max_context_chars)
        rag_answer = call_openai(openai_model, query, evidence) if evidence else "evidence insufficient"
        rows.append(
            {
                "query": query,
                "gpt_only_answer": gpt_only_answer,
                "rag_answer": rag_answer,
                "retrieved_evidence_count": len(evidence),
                "unique_pmcid_count": unique_pmcid_count(evidence),
                "gpt_only_answer_length": answer_length(gpt_only_answer),
                "rag_answer_length": answer_length(rag_answer),
            }
        )
    return rows


def call_gpt_only(openai_model: str, query: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing OpenAI SDK. Install it with `pip install openai`.") from exc

    client = OpenAI(timeout=DEFAULT_OPENAI_TIMEOUT_SECONDS)
    response = client.responses.create(
        model=openai_model,
        instructions=GPT_ONLY_INSTRUCTIONS,
        input=query,
        store=False,
        **openai_reasoning_kwargs(openai_model),
    )
    return response.output_text.strip()


def unique_pmcid_count(evidence: list[dict[str, Any]]) -> int:
    pmcids = {
        str(item.get("node_id", "")).split(":", 1)[0]
        for item in evidence
        if item.get("node_id")
    }
    return len(pmcids)


def answer_length(answer: str) -> int:
    return len(answer.split())


def print_report(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows, start=1):
        print(f"\n{'=' * 80}")
        print(f"Question {index}: {row['query']}")
        print("=" * 80)
        print("\nGPT-only answer:")
        print(row["gpt_only_answer"])
        print("\nRAG answer:")
        print(row["rag_answer"])
        print("\nMetrics:")
        print(f"retrieved evidence count: {row['retrieved_evidence_count']}")
        print(f"unique PMCID count: {row['unique_pmcid_count']}")
        print(f"GPT-only answer length: {row['gpt_only_answer_length']} words")
        print(f"RAG answer length: {row['rag_answer_length']} words")

    print(f"\n{'=' * 80}")
    print("Manual comparison")
    print("=" * 80)
    print("For each question, compare GPT-only vs RAG+GPT on:")
    print("- correctness")
    print("- specificity")
    print("- literature support")
    print("- novelty")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark GPT-only answering against RAG+GPT answering.")
    parser.add_argument("--query", action="append", help="Question to benchmark. Can be passed multiple times.")
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

    rows = run_benchmark(
        queries=args.query or DEFAULT_QUERIES,
        nodes_path=args.nodes,
        storage_dir=args.storage_dir,
        embedding_model=args.embedding_model,
        openai_model=args.openai_model,
        top_k=args.top_k,
        exclude_section_types=args.exclude_section_type,
        max_context_chars=args.max_context_chars,
    )
    print_report(rows)


if __name__ == "__main__":
    main()
