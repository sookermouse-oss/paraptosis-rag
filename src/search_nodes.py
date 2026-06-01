from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_NODES_PATH = Path("data/nodes.jsonl")
DEFAULT_QUERIES = [
    "paraptosis",
    "ER stress",
    "PI4KB",
    "calcium homeostasis",
    "breast cancer",
]
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def load_nodes(path: Path) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                nodes.append(json.loads(line))
    return nodes


def search(nodes: list[dict[str, Any]], query: str, top_k: int = 5) -> list[tuple[float, dict[str, Any]]]:
    documents = [_search_text(node) for node in nodes]
    tokenized_documents = [tokenize(document) for document in documents]
    query_tokens = tokenize(query)
    scores = bm25_scores(tokenized_documents, query_tokens)
    scores = [
        score + phrase_boost(node, query)
        for score, node in zip(scores, nodes, strict=True)
    ]

    ranked = sorted(
        ((score, node) for score, node in zip(scores, nodes, strict=True) if score > 0),
        key=lambda item: item[0],
        reverse=True,
    )
    return ranked[:top_k]


def filter_nodes(
    nodes: list[dict[str, Any]],
    exclude_section_types: list[str] | None,
    include_content_types: list[str] | None = None,
    exclude_content_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    excluded = {section_type.casefold() for section_type in exclude_section_types or []}
    included_content = {content_type.casefold() for content_type in include_content_types or []}
    excluded_content = {content_type.casefold() for content_type in exclude_content_types or []}
    return [
        node
        for node in nodes
        if (node.get("section_type") or "").casefold() not in excluded
        and (not included_content or (node.get("content_type") or "fulltext").casefold() in included_content)
        and (node.get("content_type") or "fulltext").casefold() not in excluded_content
    ]


def bm25_scores(
    documents: list[list[str]],
    query_tokens: list[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    if not documents or not query_tokens:
        return [0.0 for _ in documents]

    doc_freqs: Counter[str] = Counter()
    term_freqs = [Counter(document) for document in documents]
    for term_counts in term_freqs:
        doc_freqs.update(term_counts.keys())

    doc_lengths = [len(document) for document in documents]
    avg_doc_length = sum(doc_lengths) / len(doc_lengths)
    query_counts = Counter(query_tokens)
    scores: list[float] = []

    for term_counts, doc_length in zip(term_freqs, doc_lengths, strict=True):
        score = 0.0
        for term, query_count in query_counts.items():
            term_freq = term_counts.get(term, 0)
            if term_freq == 0:
                continue
            doc_freq = doc_freqs[term]
            idf = math.log(1 + (len(documents) - doc_freq + 0.5) / (doc_freq + 0.5))
            denominator = term_freq + k1 * (1 - b + b * doc_length / avg_doc_length)
            score += query_count * idf * (term_freq * (k1 + 1) / denominator)
        scores.append(score)
    return scores


def tokenize(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(text)]


def phrase_boost(node: dict[str, Any], query: str) -> float:
    query = " ".join(query.casefold().split())
    if not query:
        return 0.0

    text = " ".join((node.get("text") or "").casefold().split())
    metadata = " ".join(
        str(part).casefold()
        for part in (
            node.get("title", ""),
            node.get("section_title", ""),
            " ".join(node.get("keywords", [])),
        )
        if part
    )
    score = 0.0
    if query in text:
        score += 3.0
    if query in metadata:
        score += 1.0
    return score


def _search_text(node: dict[str, Any]) -> str:
    keywords = " ".join(node.get("keywords", []))
    return " ".join(
        part
        for part in (
            node.get("title", ""),
            node.get("section_title", ""),
            keywords,
            node.get("text", ""),
        )
        if part
    )


def _preview(text: str, max_chars: int = 300) -> str:
    text = " ".join(text.split())
    return text[:max_chars].rstrip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Search local ingestion nodes with BM25.")
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_PATH)
    parser.add_argument("--query", action="append", help="Query to search. Can be passed multiple times.")
    parser.add_argument("--top-k", type=int, default=5)
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
    queries = args.query or DEFAULT_QUERIES

    for query in queries:
        print(f"\n=== {query} ===")
        results = search(nodes, query, args.top_k)
        if not results:
            print("No results.")
            continue
        for score, node in results:
            print(
                json.dumps(
                    {
                        "score": round(score, 4),
                        "title": node.get("title"),
                        "content_type": node.get("content_type"),
                        "section_title": node.get("section_title"),
                        "preview": _preview(node.get("text", "")),
                    },
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    main()
