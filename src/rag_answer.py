from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
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
DEFAULT_OPENAI_REASONING_EFFORT = "medium"
DEFAULT_OPENAI_TIMEOUT_SECONDS = 120
DEFAULT_EXCLUDED_SECTION_TYPES = ["methods"]
DEFAULT_TERMINOLOGY_PATH = Path("config/terminology_zh_en.json")
DEFAULT_ANSWER_STYLE = "scientist"
ANSWER_STYLE_CHOICES = ("evidence", "scientist")
MAX_CONTEXT_CHARS_PER_NODE = 1600
CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
NODE_ID_RE = re.compile(r"[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+")
CITATION_BRACKET_RE = re.compile(
    rf"\[([^\[\]]*(?:{NODE_ID_RE.pattern}|PMC\d+|paper_[A-Za-z0-9_-]+)[^\[\]]*)\]"
)
QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "between",
    "can",
    "does",
    "how",
    "in",
    "is",
    "of",
    "or",
    "relationship",
    "role",
    "the",
    "to",
    "what",
    "with",
}
QUERY_ANCHOR_TERMS = {"paraptosis"}
# English terms that the rewrite model frequently but wrongly substitutes for a
# glossary term. When the source query maps to the canonical key, any of these
# siblings appearing in the rewrite is a mistranslation and is stripped out.
# Extend per anchor as new confusions show up in eval.
CONFUSABLE_TRANSLATIONS: dict[str, tuple[str, ...]] = {
    "paraptosis": (
        "parthanatos",
        "secondary apoptosis",
        "para-apoptosis",
        "paraapoptosis",
        "aponecrosis",
    ),
}
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
- Cite the exact full node_id after every key claim, e.g. [PMC123:sec1:chunk0].
- Do not shorten node_id citations to PMCID-only citations.
- Keep the answer concise and mechanistic.
- Do not begin with or include the label "Short answer:".
- Return only the answer text. Do not print an Evidence section.
"""

SCIENTIST_SYSTEM_PROMPT = """You answer biomedical RAG questions using only the retrieved context.

Rules:
- Use only the retrieved nodes below.
- If the retrieved nodes do not support an answer, say so naturally.
- Do not invent citations, papers, mechanisms, or claims.
- Cite the exact full node_id for key claims, e.g. [PMC123:sec1:chunk0].
- Do not shorten node_id citations to PMCID-only citations.
- Answer like a biomedical researcher explaining to another researcher.
- Start with the direct answer.
- Synthesize evidence across nodes instead of listing node-by-node.
- Do not follow the order of retrieved evidence mechanically.
- Use citations for key claims, but do not cite every sentence.
- Prefer clear mechanism flow: trigger → pathway → phenotype → implication.
- If evidence is limited, say so naturally.
- For Chinese answers, use fluent academic Chinese, not translationese.
- Avoid repeating phrases like “现有证据指出” in every paragraph.
- Keep the answer concise and mechanistic.
- Keep the answer compact. Prefer 3–5 short paragraphs unless the question asks for detail.
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
    include_content_types: list[str] | None = None,
    exclude_content_types: list[str] | None = None,
    show_context: bool = False,
    save_context: bool = False,
    debug_context_path: Path = Path("data/debug_context.txt"),
    answer_style: str = DEFAULT_ANSWER_STYLE,
) -> tuple[str, list[dict[str, Any]]]:
    result = run_rag_pipeline(
        query=query,
        nodes_path=nodes_path,
        storage_dir=storage_dir,
        embedding_model=embedding_model,
        openai_model=openai_model,
        top_k=top_k,
        exclude_section_types=exclude_section_types,
        include_content_types=include_content_types,
        exclude_content_types=exclude_content_types,
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
        answer_style=answer_style,
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
    include_content_types: list[str] | None,
    exclude_content_types: list[str] | None,
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
    answer_style: str = DEFAULT_ANSWER_STYLE,
) -> dict[str, Any]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set.")

    all_nodes = load_nodes(nodes_path)
    nodes = filter_nodes(all_nodes, exclude_section_types, include_content_types, exclude_content_types)
    node_lookup = {node["node_id"]: node for node in all_nodes}
    index = load_index(storage_dir, embedding_model)
    original_query = query
    normalized_query, query_audit = normalize_retrieval_query(
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
        include_content_types=include_content_types,
        exclude_content_types=exclude_content_types,
        bm25_weight=bm25_weight,
        embedding_weight=embedding_weight,
        penalize_single_source=penalize_single_source,
        bm25_only_penalty=bm25_only_penalty,
        embedding_only_penalty=embedding_only_penalty,
    )
    evidence = enrich_evidence(results, node_lookup, max_context_chars)
    if show_context:
        print(format_query_audit(query_audit))
        print_context_debug(original_query, normalized_query, evidence)
    if save_context:
        save_context_debug(debug_context_path, original_query, normalized_query, evidence)
    if not evidence:
        return {
            "original_query": original_query,
            "normalized_query": normalized_query,
            "query_audit": asdict(query_audit),
            "answer": insufficient_answer(original_query),
            "evidence": [],
        }

    answer = call_openai(
        openai_model,
        original_query,
        evidence,
        normalized_query=normalized_query,
        answer_style=answer_style,
    )
    return {
        "original_query": original_query,
        "normalized_query": normalized_query,
        "query_audit": asdict(query_audit),
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
                "content_type": result.get("content_type") or node.get("content_type", "fulltext"),
                "pmcid": node.get("pmcid") or pmcid_from_node_id(result.get("node_id")),
                "year": result.get("year") or node.get("year"),
                "cited_by_count": result.get("cited_by_count") or node.get("cited_by_count"),
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


@dataclass
class QueryAudit:
    """Snapshot of one query-rewrite, for benchmark visibility into drift.

    normalized == the retrieval query actually used (after the guard).
    protected  == canonical EN terms whose ZH key was in the source query.
    removed    == confusable mistranslations the guard stripped.
    added      == glossary EN terms in the rewrite whose ZH key was NOT in the
                  source (off-topic drift, e.g. an extra cell-death type).
    missing    == protected terms the guard had to re-append because the rewrite
                  dropped them.
    """

    original: str
    normalized: str
    protected: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def normalize_retrieval_query(
    original_query: str,
    openai_model: str,
    no_query_normalization: bool,
) -> tuple[str, QueryAudit]:
    if no_query_normalization or not contains_chinese(original_query):
        audit = QueryAudit(original=original_query, normalized=original_query)
        return original_query, audit
    protected_pairs = protected_terminology_for_query(original_query)

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing OpenAI SDK. Install it with `pip install openai`.") from exc

    client = OpenAI(timeout=DEFAULT_OPENAI_TIMEOUT_SECONDS)
    response = create_openai_response(
        client,
        stage="query normalization",
        model=openai_model,
        instructions=build_query_normalization_instructions(protected_pairs),
        input_text=original_query,
    )
    raw_rewrite = cleanup_normalized_query(response.output_text)
    if not raw_rewrite:
        raise SystemExit("Failed to normalize the Chinese query into an English retrieval query.")
    final_query = ensure_protected_terminology(raw_rewrite, protected_pairs)
    audit = audit_query_rewrite(original_query, raw_rewrite, final_query, protected_pairs)
    return final_query, audit


def audit_query_rewrite(
    original: str,
    raw_rewrite: str,
    final_query: str,
    protected_pairs: list[tuple[str, str]],
) -> QueryAudit:
    glossary = load_project_terminology()
    source_zh = {zh_term for zh_term, _ in protected_pairs}
    protected = [en_term for _, en_term in protected_pairs]

    removed: list[str] = []
    for _zh_term, en_term in protected_pairs:
        for wrong_term in CONFUSABLE_TRANSLATIONS.get(en_term, ()):
            if english_term_present(raw_rewrite, wrong_term) and wrong_term not in removed:
                removed.append(wrong_term)

    missing = [
        en_term for en_term in protected if not english_term_present(raw_rewrite, en_term)
    ]

    # Off-topic drift: a glossary term shows up in the rewrite, but its Chinese
    # key was never in the source query. Audited, not deleted (see notes).
    added = [
        en_term
        for zh_term, en_term in glossary.items()
        if zh_term not in source_zh and english_term_present(final_query, en_term)
    ]

    return QueryAudit(
        original=original,
        normalized=final_query,
        protected=protected,
        removed=removed,
        added=added,
        missing=missing,
    )


def format_query_audit(audit: QueryAudit) -> str:
    def show(items: list[str]) -> str:
        return ", ".join(items) if items else "None"

    return (
        f"Original:\n{audit.original}\n\n"
        f"Rewrite:\n{audit.normalized}\n\n"
        f"Protected:\n{show(audit.protected)}\n\n"
        f"Removed (mistranslations):\n{show(audit.removed)}\n\n"
        f"Added (off-topic terms):\n{show(audit.added)}\n\n"
        f"Missing (re-appended):\n{show(audit.missing)}"
    )


def build_query_normalization_instructions(
    protected_pairs: list[tuple[str, str]],
) -> str:
    """Append hard terminology constraints so rewrite uses canonical terms."""
    if not protected_pairs:
        return QUERY_NORMALIZATION_SYSTEM_PROMPT
    lines: list[str] = []
    for zh_term, en_term in protected_pairs:
        lines.append(f'- Translate "{zh_term}" exactly as "{en_term}".')
        forbidden = CONFUSABLE_TRANSLATIONS.get(en_term, ())
        if forbidden:
            joined = ", ".join(f'"{term}"' for term in forbidden)
            lines.append(
                f'  Never render "{zh_term}" as {joined}, or any other cell-death term.'
            )
    glossary_block = (
        "Mandatory terminology (use these exact English terms, no synonyms):\n"
        + "\n".join(lines)
    )
    return f"{QUERY_NORMALIZATION_SYSTEM_PROMPT}\n{glossary_block}\n"


def load_project_terminology(path: Path = DEFAULT_TERMINOLOGY_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Terminology map must be a JSON object: {path}")
    terminology: dict[str, str] = {}
    for zh_term, en_term in data.items():
        if not isinstance(zh_term, str) or not isinstance(en_term, str):
            raise ValueError(f"Terminology keys and values must be strings: {path}")
        zh_term = zh_term.strip()
        en_term = en_term.strip()
        if zh_term and en_term:
            terminology[zh_term] = en_term
    return terminology


def protected_terminology_for_query(query: str) -> list[tuple[str, str]]:
    terminology = load_project_terminology()
    occupied = [False] * len(query)
    protected_pairs: list[tuple[str, str]] = []
    for zh_term, en_term in sorted(terminology.items(), key=lambda item: len(item[0]), reverse=True):
        start = 0
        while True:
            index = query.find(zh_term, start)
            if index < 0:
                break
            end = index + len(zh_term)
            if not any(occupied[index:end]):
                protected_pairs.append((zh_term, en_term))
                for position in range(index, end):
                    occupied[position] = True
                break
            start = end
    return protected_pairs


def ensure_protected_terminology(
    normalized_query: str,
    protected_pairs: list[tuple[str, str]],
) -> str:
    result = normalized_query
    # Remove confusable mistranslations the rewrite may have introduced
    # (for example, 副凋亡 -> "parthanatos"), so retrieval is not pulled off-topic.
    for _zh_term, en_term in protected_pairs:
        for wrong_term in CONFUSABLE_TRANSLATIONS.get(en_term, ()):
            result = remove_english_term(result, wrong_term)
    result = " ".join(result.split()).strip()

    missing_terms: list[str] = []
    for _zh_term, en_term in protected_pairs:
        if not english_term_present(result, en_term) and en_term not in missing_terms:
            missing_terms.append(en_term)
    if missing_terms:
        result = result.rstrip(" \t\r\n?.!:;")
        result = f"{result} {' '.join(missing_terms)}".strip()
    return result


def remove_english_term(text: str, term: str) -> str:
    # Strip the term plus an optional immediately-following parenthetical gloss,
    # e.g. "parthanatos (PARP-dependent cell death)" -> "".
    pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])(\s*\([^()]*\))?"
    return re.sub(pattern, " ", text, flags=re.IGNORECASE)


def english_term_present(text: str, term: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


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
    answer_style: str = DEFAULT_ANSWER_STYLE,
) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing OpenAI SDK. Install it with `pip install openai`.") from exc

    client = OpenAI(timeout=DEFAULT_OPENAI_TIMEOUT_SECONDS)
    original_query = query
    normalized_query = normalized_query or query
    response = create_openai_response(
        client,
        stage="answer generation",
        model=openai_model,
        instructions=build_system_prompt(original_query, answer_style),
        input_text=build_user_prompt(original_query, normalized_query, evidence),
    )
    return sanitize_answer_citations(response.output_text.strip(), evidence)


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
            **openai_reasoning_kwargs(model),
        )
    except Exception as exc:
        if is_openai_network_error(exc):
            raise SystemExit(openai_error_message(stage, exc)) from exc
        raise


def openai_reasoning_kwargs(model: str) -> dict[str, Any]:
    if model == DEFAULT_OPENAI_MODEL:
        return {"reasoning": {"effort": DEFAULT_OPENAI_REASONING_EFFORT}}
    return {}


def sanitize_answer_citations(answer: str, evidence: list[dict[str, Any]]) -> str:
    allowed_ids = {
        str(item.get("node_id"))
        for item in evidence
        if item.get("node_id")
    }
    if not allowed_ids:
        return strip_all_node_citations(answer)

    def replace_bracket(match: re.Match[str]) -> str:
        citation_text = match.group(1)
        valid_ids = []
        for node_id in NODE_ID_RE.findall(citation_text):
            if node_id in allowed_ids and node_id not in valid_ids:
                valid_ids.append(node_id)
        if not valid_ids:
            return ""
        return "[" + "; ".join(valid_ids) + "]"

    cleaned = CITATION_BRACKET_RE.sub(replace_bracket, answer)
    return cleanup_citation_whitespace(cleaned)


def strip_all_node_citations(answer: str) -> str:
    return cleanup_citation_whitespace(CITATION_BRACKET_RE.sub("", answer))


def cleanup_citation_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


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


def build_system_prompt(original_query: str, answer_style: str = DEFAULT_ANSWER_STYLE) -> str:
    if answer_style not in ANSWER_STYLE_CHOICES:
        raise ValueError(f"Unknown answer style: {answer_style}")
    answer_language = answer_language_for_query(original_query)
    base_prompt = SCIENTIST_SYSTEM_PROMPT if answer_style == "scientist" else SYSTEM_PROMPT
    parts = [
        base_prompt.strip(),
        "",
        f"The original query is in {answer_language}. Answer in {answer_language}.",
        "Keep node_id citations in the original English format and include the full node_id, not just the PMCID.",
    ]
    if contains_chinese(original_query):
        parts.extend(["", CHINESE_TERMINOLOGY_PROMPT.strip()])
    return "\n".join(parts)


def build_user_prompt(original_query: str, normalized_query: str, evidence: list[dict[str, Any]]) -> str:
    context_blocks = []
    allowed_citations = [
        str(item.get("node_id"))
        for item in evidence
        if item.get("node_id")
    ]
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
            "Every key conclusion must include one or more exact full node_id citations, e.g. [PMC123:sec1:chunk0].",
            "Do not cite only the PMCID when a full node_id is available.",
            "Allowed citation node_ids:",
            "\n".join(allowed_citations),
            "Never cite a node_id that is not in the allowed citation list.",
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
                f"Content Type: {item.get('content_type')}",
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


def retrieval_quality(evidence: list[dict[str, Any]], query: str = "") -> dict[str, Any]:
    scores = [score for score in (to_float(item.get("score")) for item in evidence) if score is not None]
    top1_score = scores[0] if scores else 0.0
    top5 = scores[:5]
    top5_avg_score = sum(top5) / len(top5) if top5 else 0.0
    unique_pmcids = {item.get("pmcid") for item in evidence if item.get("pmcid")}
    top8_unique_pmcids = {item.get("pmcid") for item in evidence[:8] if item.get("pmcid")}
    content_type_distribution = Counter(item.get("content_type") or "missing" for item in evidence)
    section_type_distribution = Counter(item.get("section_type") or "missing" for item in evidence)
    query_coverage = evidence_query_coverage(query, evidence)
    diversity_label = evidence_diversity_label(len(top8_unique_pmcids))

    if top1_score >= 0.85 and top5_avg_score >= 0.45:
        label = "HIGH"
    elif top1_score >= 0.6 and top5_avg_score >= 0.25:
        label = "MEDIUM"
    else:
        label = "LOW"

    return {
        "label": label,
        "retrieval_strength": label,
        "evidence_diversity": diversity_label,
        "top1_score": top1_score,
        "top5_avg_score": top5_avg_score,
        "query_coverage": query_coverage,
        "unique_pmcid_count": len(unique_pmcids),
        "top8_unique_pmcid_count": len(top8_unique_pmcids),
        "content_type_distribution": dict(content_type_distribution),
        "section_type_distribution": dict(section_type_distribution),
    }


def evidence_diversity_label(top8_unique_pmcid_count: int) -> str:
    if top8_unique_pmcid_count >= 5:
        return "HIGH"
    if top8_unique_pmcid_count >= 3:
        return "MEDIUM"
    return "LOW"


def to_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def evidence_query_coverage(query: str, evidence: list[dict[str, Any]]) -> float:
    terms = important_query_terms(query)
    if not terms:
        return 1.0 if evidence else 0.0
    haystack = " ".join(
        str(part).casefold()
        for item in evidence[:5]
        for part in (
            item.get("title", ""),
            item.get("section_title", ""),
            item.get("quote", ""),
        )
        if part
    )
    matched = sum(1 for term in terms if term in haystack)
    return matched / len(terms)


def important_query_terms(query: str) -> list[str]:
    terms = []
    for token in QUERY_TOKEN_RE.findall(query.casefold()):
        if len(token) < 3:
            continue
        if token in QUERY_STOPWORDS or token in QUERY_ANCHOR_TERMS:
            continue
        if token not in terms:
            terms.append(token)
    return terms


def print_retrieval_confidence(evidence: list[dict[str, Any]], normalized_query: str) -> None:
    quality = retrieval_quality(evidence, normalized_query)
    print(f"\nRetrieval strength: {quality['retrieval_strength']}")
    print(f"top1_score: {quality['top1_score']:.4f}")
    print(f"top5_avg_score: {quality['top5_avg_score']:.4f}")
    print(f"Evidence diversity: {quality['evidence_diversity']}")
    print(f"unique_pmcid_count: {quality['unique_pmcid_count']}")
    print(f"top8_unique_pmcid_count: {quality['top8_unique_pmcid_count']}")
    print(f"query_coverage: {quality['query_coverage']:.4f}")
    print(f"content_type distribution: {quality['content_type_distribution']}")
    print(f"section_type distribution: {quality['section_type_distribution']}")
    if quality["retrieval_strength"] == "LOW":
        print("Warning: retrieval strength is low. The RAG answer may be less reliable.")


def print_answer(answer: str, evidence: list[dict[str, Any]], original_query: str, normalized_query: str) -> None:
    print(f"Original query: {original_query}")
    print(f"Normalized retrieval query: {normalized_query}")
    print_retrieval_confidence(evidence, normalized_query)
    print("\nAnswer:")
    print(answer)
    print("\nEvidence:")
    for index, item in enumerate(evidence, start=1):
        print(f"{index}. {item.get('node_id')}")
        print(f"   title: {item.get('title')}")
        print(f"   content_type: {item.get('content_type')}")
        print(f"   year: {item.get('year')}")
        print(f"   cited_by_count: {item.get('cited_by_count')}")
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
    parser.add_argument("--max-context-chars", type=int, default=MAX_CONTEXT_CHARS_PER_NODE)
    parser.add_argument(
        "--answer-style",
        choices=ANSWER_STYLE_CHOICES,
        default=DEFAULT_ANSWER_STYLE,
        help="Answer style. 'evidence' keeps the current evidence-forward prompt; 'scientist' synthesizes like a biomedical researcher.",
    )
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
        include_content_types=args.include_content_type,
        exclude_content_types=args.exclude_content_type,
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
        answer_style=args.answer_style,
    )
    print_answer(
        result["answer"],
        result["evidence"],
        result["original_query"],
        result["normalized_query"],
    )


if __name__ == "__main__":
    main()
