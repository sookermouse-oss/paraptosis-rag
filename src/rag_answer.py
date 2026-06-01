from __future__ import annotations

import argparse
import os
import re
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
CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
OPENAI_NETWORK_ERROR_HINT = (
    "OpenAI request failed before a response was returned. "
    "This is usually a network/proxy issue, an unavailable upstream service, or a timeout. "
    "Retry the command after the proxy/network recovers. For Chinese queries, "
    "you can pass --no-query-normalization to skip the OpenAI rewrite step, but retrieval quality may drop."
)
QUERY_NORMALIZATION_SYSTEM_PROMPT = """Rewrite the user's Chinese biomedical question into a concise English retrieval query for literature search.

Rules:
- Preserve biomedical entities, gene names, diseases, pathways, drugs, and abbreviations.
- Translate faithfully and keep the original meaning.
- Prefer a single concise English question or retrieval phrase.
- Use standard biomedical English terminology.
- Output only the rewritten English query.
- Do not add quotes, bullets, explanations, or labels.
"""


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

CHINESE_TERMINOLOGY_PROMPT = """Chinese terminology rules:
- Translate "paraptosis" as "副凋亡"; do not transliterate it as "帕拉托西斯".
- Translate "apoptosis" as "凋亡", "ferroptosis" as "铁死亡", "pyroptosis" as "焦亡", and "necroptosis" as "坏死性凋亡".
- Translate "ER stress" or "endoplasmic reticulum stress" as "内质网应激".
- Prefer "不依赖 caspase" over "无半胱天冬酶依赖".
- Keep gene/protein/drug names, pathway abbreviations, and node_id citations unchanged.
- If the user's Chinese query already uses a domain term, keep that Chinese term in the answer.
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
    result = run_rag_pipeline(
        query=query,
        nodes_path=nodes_path,
        storage_dir=storage_dir,
        embedding_model=embedding_model,
        openai_model=openai_model,
        top_k=top_k,
        exclude_section_types=exclude_section_types,
        bm25_weight=bm25_weight,
        embedding_weight=embedding_weight,
        penalize_single_source=penalize_single_source,
        bm25_only_penalty=bm25_only_penalty,
        embedding_only_penalty=embedding_only_penalty,
        max_context_chars=max_context_chars,
        no_query_normalization=False,
        show_context=show_context,
        save_context=save_context,
        debug_context_path=debug_context_path,
    )
    return result["answer"], result["evidence"]


def run_rag_pipeline(
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
    no_query_normalization: bool,
    show_context: bool,
    save_context: bool,
    debug_context_path: Path,
) -> dict[str, Any]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set.")

    all_nodes = load_nodes(nodes_path)
    nodes = filter_nodes(all_nodes, exclude_section_types)
    node_lookup = {node["node_id"]: node for node in all_nodes}
    index = load_index(storage_dir, embedding_model)
    original_query = query
    normalized_query = normalize_retrieval_query(
        original_query,
        openai_model,
        no_query_normalization=no_query_normalization,
    )

    _, _, results = hybrid_search(
        nodes=nodes,
        index=index,
        query=normalized_query,
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
        print_context_debug(original_query, normalized_query, evidence)
    if save_context:
        save_context_debug(debug_context_path, original_query, normalized_query, evidence)
    if not evidence:
        return {
            "original_query": original_query,
            "normalized_query": normalized_query,
            "answer": insufficient_answer(original_query),
            "evidence": [],
        }

    answer = call_openai(
        openai_model,
        original_query,
        evidence,
        normalized_query=normalized_query,
    )
    return {
        "original_query": original_query,
        "normalized_query": normalized_query,
        "answer": answer,
        "evidence": evidence,
    }


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


def contains_chinese(text: str) -> bool:
    return CHINESE_CHAR_RE.search(text) is not None


def normalize_retrieval_query(
    original_query: str,
    openai_model: str,
    no_query_normalization: bool,
) -> str:
    if no_query_normalization or not contains_chinese(original_query):
        return original_query

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing OpenAI SDK. Install it with `pip install openai`.") from exc

    client = OpenAI()
    response = create_openai_response(
        client,
        stage="query normalization",
        model=openai_model,
        instructions=QUERY_NORMALIZATION_SYSTEM_PROMPT,
        input_text=original_query,
    )
    normalized = cleanup_normalized_query(response.output_text)
    if not normalized:
        raise SystemExit("Failed to normalize the Chinese query into an English retrieval query.")
    return normalized


def cleanup_normalized_query(text: str) -> str:
    cleaned = " ".join(text.split()).strip()
    cleaned = re.sub(
        r"^(?:retrieval query|normalized retrieval query|query)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip(" \t\r\n\"'`")
    return cleaned


def answer_language_for_query(original_query: str) -> str:
    return "Chinese" if contains_chinese(original_query) else "English"


def insufficient_answer(original_query: str) -> str:
    return "证据不足" if contains_chinese(original_query) else "evidence insufficient"


def call_openai(
    openai_model: str,
    query: str,
    evidence: list[dict[str, Any]],
    normalized_query: str | None = None,
) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing OpenAI SDK. Install it with `pip install openai`.") from exc

    client = OpenAI()
    original_query = query
    normalized_query = normalized_query or query
    response = create_openai_response(
        client,
        stage="answer generation",
        model=openai_model,
        instructions=build_system_prompt(original_query),
        input_text=build_user_prompt(original_query, normalized_query, evidence),
    )
    return response.output_text.strip()


def create_openai_response(
    client: Any,
    stage: str,
    model: str,
    instructions: str,
    input_text: str,
) -> Any:
    try:
        return client.responses.create(
            model=model,
            instructions=instructions,
            input=input_text,
            store=False,
        )
    except Exception as exc:
        if is_openai_network_error(exc):
            raise SystemExit(openai_error_message(stage, exc)) from exc
        raise


def is_openai_network_error(exc: Exception) -> bool:
    module = type(exc).__module__
    name = type(exc).__name__
    return module.startswith(("openai", "httpx", "httpcore")) or name in {
        "APIConnectionError",
        "APITimeoutError",
        "APIStatusError",
        "APIError",
        "RateLimitError",
        "ProxyError",
        "ConnectError",
        "ReadTimeout",
        "ConnectTimeout",
    }


def openai_error_message(stage: str, exc: Exception) -> str:
    return "\n".join(
        [
            f"OpenAI {stage} failed.",
            f"{type(exc).__name__}: {exc}",
            OPENAI_NETWORK_ERROR_HINT,
        ]
    )


def build_system_prompt(original_query: str) -> str:
    answer_language = answer_language_for_query(original_query)
    parts = [
        SYSTEM_PROMPT.strip(),
        "",
        f"The original query is in {answer_language}. Answer in {answer_language}.",
        "Keep node_id citations in the original English format.",
    ]
    if contains_chinese(original_query):
        parts.extend(["", CHINESE_TERMINOLOGY_PROMPT.strip()])
    return "\n".join(parts)


def build_user_prompt(original_query: str, normalized_query: str, evidence: list[dict[str, Any]]) -> str:
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
            f"Original query: {original_query}",
            f"Normalized retrieval query: {normalized_query}",
            "Retrieved nodes:",
            "\n\n".join(context_blocks),
            "Answer the original query using only these nodes.",
            f'If the nodes are not enough, say "{insufficient_answer(original_query)}".',
            'Do not use the phrase "Short answer:".',
            "Every key conclusion must include one or more node_id citations.",
        ]
    )


def print_context_debug(query: str, normalized_query: str, evidence: list[dict[str, Any]]) -> None:
    print(build_context_debug_report(query, normalized_query, evidence, include_prompt=False))


def save_context_debug(path: Path, query: str, normalized_query: str, evidence: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_context_debug_report(query, normalized_query, evidence, include_prompt=True), encoding="utf-8")


def build_context_debug_report(
    query: str,
    normalized_query: str,
    evidence: list[dict[str, Any]],
    include_prompt: bool,
) -> str:
    lines: list[str] = [
        "========================================",
        "Retrieved Context",
        "========================================",
        "",
        f"Original query: {query}",
        f"Normalized retrieval query: {normalized_query}",
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
    prompt = build_user_prompt(query, normalized_query, evidence)
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
            "Original query:",
            query,
            "",
            "Normalized retrieval query:",
            normalized_query,
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


def print_answer(answer: str, evidence: list[dict[str, Any]], original_query: str, normalized_query: str) -> None:
    print(f"Original query: {original_query}")
    print(f"Normalized retrieval query: {normalized_query}")
    print("\nAnswer:")
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
        "--no-query-normalization",
        action="store_true",
        help="Disable Chinese query normalization before retrieval.",
    )
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

    result = run_rag_pipeline(
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
        no_query_normalization=args.no_query_normalization,
        show_context=args.show_context,
        save_context=args.save_context,
        debug_context_path=Path("data/debug_context.txt"),
    )
    print_answer(
        result["answer"],
        result["evidence"],
        result["original_query"],
        result["normalized_query"],
    )


if __name__ == "__main__":
    main()
