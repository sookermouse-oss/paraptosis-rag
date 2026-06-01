from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from llamaindex_retrieval import (
    DEFAULT_MODEL,
    DEFAULT_NODES_PATH,
    DEFAULT_STORAGE_DIR,
    format_result,
    load_index,
    node_metadata,
    content_type_counts,
    retrieve as embedding_retrieve,
    section_type_counts,
)
from search_nodes import DEFAULT_QUERIES, filter_nodes, load_nodes, search as bm25_search


DEFAULT_CANDIDATE_K = 30
DEFAULT_TOP_K = 10
DEFAULT_BM25_WEIGHT = 0.2
DEFAULT_EMBEDDING_WEIGHT = 0.8
DEFAULT_BM25_ONLY_PENALTY = 0.4
DEFAULT_EMBEDDING_ONLY_PENALTY = 0.8
DEFAULT_BENCHMARK_QUERIES = [
    "How does ER stress induce paraptosis?",
    "What is the role of PI4KB in paraptosis?",
    "paraptosis and calcium homeostasis",
    "paraptosis in breast cancer prognosis",
    "paraptosis drug resistance",
]


def hybrid_search(
    nodes: list[dict[str, Any]],
    index: Any,
    query: str,
    top_k: int,
    exclude_section_types: list[str],
    include_content_types: list[str] | None = None,
    exclude_content_types: list[str] | None = None,
    bm25_weight: float = DEFAULT_BM25_WEIGHT,
    embedding_weight: float = DEFAULT_EMBEDDING_WEIGHT,
    penalize_single_source: bool = True,
    bm25_only_penalty: float = DEFAULT_BM25_ONLY_PENALTY,
    embedding_only_penalty: float = DEFAULT_EMBEDDING_ONLY_PENALTY,
    max_chunks_per_pmcid: int | None = None,
    candidate_k: int = DEFAULT_CANDIDATE_K,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    bm25_results = [
        format_result(score, node_metadata(node), node.get("text", ""))
        for score, node in bm25_search(nodes, query, candidate_k)
    ]
    embedding_results = embedding_retrieve(
        index,
        query,
        candidate_k,
        exclude_section_types,
        include_content_types,
        exclude_content_types,
    )

    hybrid_results = rank_hybrid_results(
        bm25_results,
        embedding_results,
        bm25_weight,
        embedding_weight,
        penalize_single_source,
        bm25_only_penalty,
        embedding_only_penalty,
        max_chunks_per_pmcid,
    )
    return bm25_results[:top_k], embedding_results[:top_k], hybrid_results[:top_k]


def rank_hybrid_results(
    bm25_results: list[dict[str, Any]],
    embedding_results: list[dict[str, Any]],
    bm25_weight: float,
    embedding_weight: float,
    penalize_single_source: bool = True,
    bm25_only_penalty: float = DEFAULT_BM25_ONLY_PENALTY,
    embedding_only_penalty: float = DEFAULT_EMBEDDING_ONLY_PENALTY,
    max_chunks_per_pmcid: int | None = None,
) -> list[dict[str, Any]]:
    if bm25_weight < 0 or embedding_weight < 0:
        raise ValueError("Hybrid weights must be non-negative.")
    total_weight = bm25_weight + embedding_weight
    if total_weight <= 0:
        raise ValueError("At least one hybrid weight must be positive.")

    bm25_weight = bm25_weight / total_weight
    embedding_weight = embedding_weight / total_weight
    bm25_scores = {result["node_id"]: float(result["score"]) for result in bm25_results}
    embedding_scores = {result["node_id"]: float(result["score"]) for result in embedding_results}
    bm25_norm = normalize_scores(bm25_scores)
    embedding_norm = normalize_scores(embedding_scores)

    candidates: dict[str, dict[str, Any]] = {}
    for result in [*bm25_results, *embedding_results]:
        node_id = result["node_id"]
        candidates.setdefault(node_id, result)

    hybrid_results: list[dict[str, Any]] = []
    for node_id, result in candidates.items():
        bm25_score = bm25_scores.get(node_id, 0.0)
        embedding_score = embedding_scores.get(node_id, 0.0)
        source = result_source(node_id, bm25_scores, embedding_scores)
        hybrid_score = (
            bm25_weight * bm25_norm.get(node_id, 0.0)
            + embedding_weight * embedding_norm.get(node_id, 0.0)
        )
        if penalize_single_source:
            hybrid_score *= source_penalty(source, bm25_only_penalty, embedding_only_penalty)
        hybrid_results.append(
            {
                **result,
                "score": round(hybrid_score, 4),
                "bm25_score": round(bm25_score, 4),
                "embedding_score": round(embedding_score, 4),
                "hybrid_score": round(hybrid_score, 4),
                "hybrid_bm25_weight": round(bm25_weight, 4),
                "hybrid_embedding_weight": round(embedding_weight, 4),
                "hybrid_source": source,
                "bm25_only_penalty": bm25_only_penalty if penalize_single_source else 1.0,
                "embedding_only_penalty": embedding_only_penalty if penalize_single_source else 1.0,
            }
        )

    hybrid_results.sort(
        key=lambda result: (
            result["hybrid_score"],
            result["embedding_score"],
            result["bm25_score"],
        ),
        reverse=True,
    )
    return apply_pmcid_diversity(hybrid_results, max_chunks_per_pmcid)


def normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    values = list(scores.values())
    min_score = min(values)
    max_score = max(values)
    if max_score == min_score:
        return {node_id: 1.0 for node_id in scores}
    return {
        node_id: (score - min_score) / (max_score - min_score)
        for node_id, score in scores.items()
    }


def result_source(
    node_id: str,
    bm25_scores: dict[str, float],
    embedding_scores: dict[str, float],
) -> str:
    in_bm25 = node_id in bm25_scores
    in_embedding = node_id in embedding_scores
    if in_bm25 and in_embedding:
        return "both"
    if in_bm25:
        return "bm25_only"
    return "embedding_only"


def source_penalty(source: str, bm25_only_penalty: float, embedding_only_penalty: float) -> float:
    if source == "bm25_only":
        return bm25_only_penalty
    if source == "embedding_only":
        return embedding_only_penalty
    return 1.0


def apply_pmcid_diversity(
    results: list[dict[str, Any]],
    max_chunks_per_pmcid: int | None,
) -> list[dict[str, Any]]:
    if max_chunks_per_pmcid is None or max_chunks_per_pmcid <= 0:
        return results

    pmcid_counts: dict[str, int] = {}
    diverse_results: list[dict[str, Any]] = []
    for result in results:
        pmcid = result.get("pmcid") or pmcid_from_node_id(result.get("node_id"))
        count = pmcid_counts.get(pmcid, 0)
        if count >= max_chunks_per_pmcid:
            continue
        pmcid_counts[pmcid] = count + 1
        diverse_results.append(result)
    return diverse_results


def pmcid_from_node_id(node_id: Any) -> str:
    if not node_id:
        return ""
    return str(node_id).split(":", 1)[0]


def benchmark(
    nodes: list[dict[str, Any]],
    index: Any,
    queries: list[str],
    top_k: int,
    exclude_section_types: list[str],
    include_content_types: list[str],
    exclude_content_types: list[str],
    bm25_weight: float,
    embedding_weight: float,
    penalize_single_source: bool,
    bm25_only_penalty: float,
    embedding_only_penalty: float,
    max_chunks_per_pmcid: int | None,
) -> None:
    for query in queries:
        bm25_results, embedding_results, hybrid_results = hybrid_search(
            nodes,
            index,
            query,
            top_k,
            exclude_section_types,
            include_content_types,
            exclude_content_types,
            bm25_weight,
            embedding_weight,
            penalize_single_source,
            bm25_only_penalty,
            embedding_only_penalty,
            max_chunks_per_pmcid,
        )

        print(f"\n=== {query} ===")
        print("BM25 section_type distribution:", dict(section_type_counts(bm25_results)))
        print("Embedding section_type distribution:", dict(section_type_counts(embedding_results)))
        print("Hybrid section_type distribution:", dict(section_type_counts(hybrid_results)))
        print("BM25 content_type distribution:", dict(content_type_counts(bm25_results)))
        print("Embedding content_type distribution:", dict(content_type_counts(embedding_results)))
        print("Hybrid content_type distribution:", dict(content_type_counts(hybrid_results)))
        print("BM25 Top 10:")
        for result in bm25_results:
            print(json.dumps(result, ensure_ascii=False))
        print("Embedding Top 10:")
        for result in embedding_results:
            print(json.dumps(result, ensure_ascii=False))
        print("Hybrid Top 10:")
        for result in hybrid_results:
            print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hybrid retrieval with BM25 and LlamaIndex embeddings.")
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_PATH)
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE_DIR)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--query", action="append", help="Query to search. Can be passed multiple times.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--bm25-weight", type=float, default=DEFAULT_BM25_WEIGHT)
    parser.add_argument("--embedding-weight", type=float, default=DEFAULT_EMBEDDING_WEIGHT)
    parser.add_argument(
        "--penalize-single-source",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply source penalties. Enabled by default; use --no-penalize-single-source to disable.",
    )
    parser.add_argument("--bm25-only-penalty", type=float, default=DEFAULT_BM25_ONLY_PENALTY)
    parser.add_argument("--embedding-only-penalty", type=float, default=DEFAULT_EMBEDDING_ONLY_PENALTY)
    parser.add_argument(
        "--max-chunks-per-pmcid",
        type=int,
        default=None,
        help="Maximum number of returned chunks per PMCID. Disabled by default.",
    )
    parser.add_argument(
        "--exclude-section-type",
        action="append",
        default=[],
        help="Section type to exclude, e.g. methods. Can be passed multiple times.",
    )
    parser.add_argument(
        "--include-content-type",
        action="append",
        default=[],
        help="Content type to include, e.g. abstract or fulltext. Can be passed multiple times.",
    )
    parser.add_argument(
        "--exclude-content-type",
        action="append",
        default=[],
        help="Content type to exclude, e.g. abstract. Can be passed multiple times.",
    )
    args = parser.parse_args()

    nodes = filter_nodes(
        load_nodes(args.nodes),
        args.exclude_section_type,
        args.include_content_type,
        args.exclude_content_type,
    )
    index = load_index(args.storage_dir, args.model)
    queries = args.query or DEFAULT_QUERIES

    if args.query:
        for query in queries:
            print(f"\n=== {query} ===")
            _, _, hybrid_results = hybrid_search(
                nodes,
                index,
                query,
                args.top_k,
                args.exclude_section_type,
                args.include_content_type,
                args.exclude_content_type,
                args.bm25_weight,
                args.embedding_weight,
                args.penalize_single_source,
                args.bm25_only_penalty,
                args.embedding_only_penalty,
                args.max_chunks_per_pmcid,
            )
            for result in hybrid_results:
                print(json.dumps(result, ensure_ascii=False))
        return

    benchmark(
        nodes,
        index,
        DEFAULT_BENCHMARK_QUERIES,
        args.top_k,
        args.exclude_section_type,
        args.include_content_type,
        args.exclude_content_type,
        args.bm25_weight,
        args.embedding_weight,
        args.penalize_single_source,
        args.bm25_only_penalty,
        args.embedding_only_penalty,
        args.max_chunks_per_pmcid,
    )


if __name__ == "__main__":
    main()
