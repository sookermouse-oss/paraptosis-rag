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
from search_nodes import filter_nodes, load_nodes


DEFAULT_TOP_K = 8
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_EXCLUDED_SECTION_TYPES = ["methods"]
MAX_CONTEXT_CHARS_PER_NODE = 1600


SYSTEM_PROMPT = """You answer biomedical RAG questions using only the retrieved context.

Rules:
- Use only the retrieved nodes below.
- If the retrieved nodes do not support an answer, explicitly say "evidence insufficient".
- Do not invent citations, papers, mechanisms, or claims.
- Cite the node_id after every key claim, e.g. [PMC123:sec1:chunk0].
- Keep the answer concise and mechanistic.
- Do not begin with or include the label "Short answer:".
- Return only the answer text. Do not print an Evidence section.
"""


def answer_query(
    query: str,
    nodes_path: Path,
    storage_dir: Path,
    embedding_model: str,
    openai_model: str,
    top_k: int,
    exclude_section_types: list[str],
    bm25_weight: float,
    embedding_weight: float,
    penalize_single_source: bool,
    bm25_only_penalty: float,
    embedding_only_penalty: float,
    max_context_chars: int,
    show_context: bool = False,
    save_context: bool = False,
    debug_context_path: Path = Path("data/debug_context.txt"),
) -> tuple[str, list[dict[str, Any]]]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set.")

    all_nodes = load_nodes(nodes_path)
    nodes = filter_nodes(all_nodes, exclude_section_types)
    node_lookup = {node["node_id"]: node for node in all_nodes}
    index = load_index(storage_dir, embedding_model)

    _, _, results = hybrid_search(
        nodes=nodes,
        index=index,
        query=query,
        top_k=top_k,
        exclude_section_types=exclude_section_types,
        bm25_weight=bm25_weight,
        embedding_weight=embedding_weight,
        penalize_single_source=penalize_single_source,
        bm25_only_penalty=bm25_only_penalty,
        embedding_only_penalty=embedding_only_penalty,
    )
    evidence = enrich_evidence(results, node_lookup, max_context_chars)
    if show_context:
        print_context_debug(query, evidence)
    if save_context:
        save_context_debug(debug_context_path, query, evidence)
    if not evidence:
        return "evidence insufficient", []

    answer = call_openai(openai_model, query, evidence)
    return answer, evidence


def enrich_evidence(
    results: list[dict[str, Any]],
    node_lookup: dict[str, dict[str, Any]],
    max_context_chars: int,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for result in results:
        node = node_lookup.get(result["node_id"], {})
        text = " ".join((node.get("text") or result.get("preview") or "").split())
        evidence.append(
            {
                "node_id": result.get("node_id"),
                "title": result.get("title"),
                "section_title": result.get("section_title"),
                "section_type": result.get("section_type"),
                "pmcid": node.get("pmcid") or pmcid_from_node_id(result.get("node_id")),
                "score": result.get("hybrid_score", result.get("score")),
                "bm25_score": result.get("bm25_score"),
                "embedding_score": result.get("embedding_score"),
                "full_text": text,
                "quote": text[:max_context_chars].rstrip(),
            }
        )
    return evidence


def pmcid_from_node_id(node_id: Any) -> str | None:
    if not node_id:
        return None
    return str(node_id).split(":", 1)[0]


def call_openai(openai_model: str, query: str, evidence: list[dict[str, Any]]) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing OpenAI SDK. Install it with `pip install openai`.") from exc

    client = OpenAI()
    response = client.responses.create(
        model=openai_model,
        instructions=SYSTEM_PROMPT,
        input=build_user_prompt(query, evidence),
        store=False,
    )
    return response.output_text.strip()


def build_user_prompt(query: str, evidence: list[dict[str, Any]]) -> str:
    context_blocks = []
    for index, item in enumerate(evidence, start=1):
        context_blocks.append(
            "\n".join(
                [
                    f"Evidence {index}",
                    f"node_id: {item['node_id']}",
                    f"title: {item.get('title')}",
                    f"section_title: {item.get('section_title')}",
                    f"section_type: {item.get('section_type')}",
                    f"text: {item.get('quote')}",
                ]
            )
        )
    return "\n\n".join(
        [
            f"Question: {query}",
            "Retrieved nodes:",
            "\n\n".join(context_blocks),
            "Answer the question using only these nodes.",
            'If the nodes are not enough, say "evidence insufficient".',
            'Do not use the phrase "Short answer:".',
            "Every key conclusion must include one or more node_id citations.",
        ]
    )


def print_context_debug(query: str, evidence: list[dict[str, Any]]) -> None:
    print(build_context_debug_report(query, evidence, include_prompt=False))


def save_context_debug(path: Path, query: str, evidence: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_context_debug_report(query, evidence, include_prompt=True), encoding="utf-8")


def build_context_debug_report(query: str, evidence: list[dict[str, Any]], include_prompt: bool) -> str:
    lines: list[str] = [
        "========================================",
        "Retrieved Context",
        "========================================",
        "",
    ]
    for index, item in enumerate(evidence, start=1):
        lines.extend(
            [
                f"Rank: {index}",
                f"Score: {item.get('score')}",
                f"Node ID: {item.get('node_id')}",
                f"PMCID: {item.get('pmcid')}",
                f"Title: {item.get('title')}",
                f"Section Title: {item.get('section_title')}",
                f"Section Type: {item.get('section_type')}",
                "Text:",
                item.get("full_text") or item.get("quote", ""),
                "",
                "----------------------------------------",
                "",
            ]
        )

    total_context_chars = sum(len(item.get("quote", "")) for item in evidence)
    prompt = build_user_prompt(query, evidence)
    lines.extend(
        [
            "========================================",
            "Prompt Size",
            "========================================",
            "",
            f"Number of retrieved nodes: {len(evidence)}",
            f"Total context characters: {total_context_chars}",
            f"Approx tokens: {approx_tokens(prompt)}",
            "",
            "========================================",
            "Sending To OpenAI",
            "========================================",
            "",
            "Question:",
            query,
        ]
    )
    if include_prompt:
        lines.extend(
            [
                "",
                "========================================",
                "Full Prompt",
                "========================================",
                "",
                prompt,
            ]
        )
    return "\n".join(lines)


def approx_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def print_answer(answer: str, evidence: list[dict[str, Any]]) -> None:
    print("Answer:")
    print(answer)
    print("\nEvidence:")
    for index, item in enumerate(evidence, start=1):
        print(f"{index}. {item.get('node_id')}")
        print(f"   title: {item.get('title')}")
        print(f"   section_title: {item.get('section_title')}")
        print(f"   score: {item.get('score')}")
        print(f"   preview: {preview(item.get('quote', ''))}")
        print()


def preview(text: str, max_chars: int = 300) -> str:
    return " ".join(text.split())[:max_chars].rstrip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal RAG answering over local hybrid retrieval results.")
    parser.add_argument("--query", required=True)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_PATH)
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE_DIR)
    parser.add_argument("--embedding-model", default=DEFAULT_MODEL)
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--bm25-weight", type=float, default=DEFAULT_BM25_WEIGHT)
    parser.add_argument("--embedding-weight", type=float, default=DEFAULT_EMBEDDING_WEIGHT)
    parser.add_argument(
        "--penalize-single-source",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply hybrid single-source penalties. Enabled by default.",
    )
    parser.add_argument("--bm25-only-penalty", type=float, default=DEFAULT_BM25_ONLY_PENALTY)
    parser.add_argument("--embedding-only-penalty", type=float, default=DEFAULT_EMBEDDING_ONLY_PENALTY)
    parser.add_argument(
        "--exclude-section-type",
        action="append",
        default=DEFAULT_EXCLUDED_SECTION_TYPES.copy(),
        help="Section type to exclude. Defaults to methods. Can be passed multiple times.",
    )
    parser.add_argument("--max-context-chars", type=int, default=MAX_CONTEXT_CHARS_PER_NODE)
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Print retrieved context and prompt size before calling OpenAI.",
    )
    parser.add_argument(
        "--save-context",
        action="store_true",
        help="Save retrieved context and full prompt to data/debug_context.txt.",
    )
    args = parser.parse_args()

    answer, evidence = answer_query(
        query=args.query,
        nodes_path=args.nodes,
        storage_dir=args.storage_dir,
        embedding_model=args.embedding_model,
        openai_model=args.openai_model,
        top_k=args.top_k,
        exclude_section_types=args.exclude_section_type,
        bm25_weight=args.bm25_weight,
        embedding_weight=args.embedding_weight,
        penalize_single_source=args.penalize_single_source,
        bm25_only_penalty=args.bm25_only_penalty,
        embedding_only_penalty=args.embedding_only_penalty,
        max_context_chars=args.max_context_chars,
        show_context=args.show_context,
        save_context=args.save_context,
    )
    print_answer(answer, evidence)


if __name__ == "__main__":
    main()
