from __future__ import annotations

import argparse
import json
import re
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
from rag_answer import (
    CITATION_BRACKET_RE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_TIMEOUT_SECONDS,
    DEFAULT_TOP_K,
    MAX_CONTEXT_CHARS_PER_NODE,
    NODE_ID_RE,
    openai_reasoning_kwargs,
    sanitize_answer_citations,
)
from search_nodes import filter_nodes, load_nodes


DEFAULT_EXCLUDED_SECTION_TYPES = ["methods"]
DEFAULT_OUTPUT_PATH = Path("data/faithfulness.json")
DEFAULT_MARKDOWN_PATH = Path("data/faithfulness.md")
GENE_LIKE_RE = re.compile(r"\b[A-Z]{2,}[0-9]*\b")
HAS_NUMBER_RE = re.compile(r"\d")
ARROW_RE = re.compile(r"[->→]|-->")
DEATH_TERMS_RE = re.compile(
    r"paraptosis|apoptosis|ferroptosis|pyroptosis|necroptosis|autophag|parthanatos|"
    r"副凋亡|凋亡|铁死亡|焦亡|程序性坏死|自噬",
    re.IGNORECASE,
)
HEDGE_RE = re.compile(
    r"evidence insufficient|not (?:been )?(?:established|demonstrated|reported|shown)|"
    r"do(?:es)? not (?:establish|support|show|demonstrate|confirm|prove)|"
    r"remains? (?:unclear|to be|unknown)|no (?:direct |retrieved )?evidence|"
    r"not (?:fully )?(?:defined|characterized)|cannot (?:fully )?answer|"
    r"\bmay\b|\bmight\b|\bcould\b|plausibl|\bsuggests?\b|only indirect|\bindirect\b|"
    r"appears to|consistent with|in principle|\blikely\b|\bproposed\b|"
    r"证据不足|尚(?:无|未)|未(?:被)?(?:证明|报道|阐明)|仍(?:不|未)明确|无法|"
    r"可能|或许|提示|尚待|有待",
    re.IGNORECASE,
)
META_RE = re.compile(
    r"retrieved (?:evidence|nodes|context|material|set)|"
    r"the (?:provided|available) (?:evidence|context|nodes)|"
    r"检索(?:到的)?(?:证据|节点|内容|材料)|提供的(?:证据|上下文)",
    re.IGNORECASE,
)

JUDGE_SYSTEM = """You check whether a CLAIM is supported by SOURCE passages.
Return ONLY JSON: {"label": "supported" | "unsupported" | "contradicted",
"rationale": "<=30 words"}.

Rules:
- "supported" only if the source states or directly entails the claim.
- "unsupported" if the claim is not grounded in the source.
- "contradicted" if the source says the opposite.
- Synthesis across source passages is allowed.
- Outside knowledge is not allowed.
"""


@dataclass
class Claim:
    text: str
    cited_ids: list[str] = field(default_factory=list)
    substantive: bool = False
    hedge: bool = False
    meta: bool = False
    bare: bool = False
    faithfulness: str = ""
    rationale: str = ""


@dataclass
class AnswerReport:
    n_claims: int
    n_substantive: int
    n_cited: int
    n_bare: int
    n_supported: int = 0
    n_unsupported: int = 0
    n_contradicted: int = 0
    bare_claims: list[str] = field(default_factory=list)


def split_sentences(text: str) -> list[str]:
    protected = text
    for abbr in ("e.g.", "i.e.", "et al.", "vs.", "Fig.", "cf.", "approx.", "ca."):
        protected = protected.replace(abbr, abbr.replace(".", "\x00"))
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\u4e00-\u9fff])", protected)
    out: list[str] = []
    for part in parts:
        for sentence in re.split(r"(?<=。)", part):
            sentence = sentence.replace("\x00", ".").strip()
            if sentence:
                out.append(sentence)
    return out


def extract_claims(answer: str) -> list[Claim]:
    claims: list[Claim] = []
    for sentence in split_sentences(answer):
        cited_ids = list(dict.fromkeys(NODE_ID_RE.findall(sentence)))
        words = len(re.findall(r"\w+", sentence))
        hedge = bool(HEDGE_RE.search(sentence))
        meta = bool(META_RE.search(sentence))
        substantive = (
            words >= 6
            and (
                bool(GENE_LIKE_RE.search(sentence))
                or bool(DEATH_TERMS_RE.search(sentence))
                or bool(ARROW_RE.search(sentence))
                or bool(HAS_NUMBER_RE.search(sentence))
            )
        )
        bare = substantive and not cited_ids and not hedge and not meta
        claims.append(
            Claim(
                text=sentence,
                cited_ids=cited_ids,
                substantive=substantive,
                hedge=hedge,
                meta=meta,
                bare=bare,
            )
        )
    return claims


def report_claims(claims: list[Claim]) -> AnswerReport:
    substantive = [claim for claim in claims if claim.substantive]
    cited = [claim for claim in substantive if claim.cited_ids]
    bare = [claim for claim in claims if claim.bare]
    supported = [claim for claim in substantive if claim.faithfulness == "supported"]
    unsupported = [claim for claim in substantive if claim.faithfulness == "unsupported"]
    contradicted = [claim for claim in substantive if claim.faithfulness == "contradicted"]
    return AnswerReport(
        n_claims=len(claims),
        n_substantive=len(substantive),
        n_cited=len(cited),
        n_bare=len(bare),
        n_supported=len(supported),
        n_unsupported=len(unsupported),
        n_contradicted=len(contradicted),
        bare_claims=[claim.text for claim in bare],
    )


def load_eval_rows(path: Path, prefer_markdown: bool = False) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return load_checkpoint(path)
    if prefer_markdown:
        return load_markdown_rows(path)
    checkpoint = path.with_suffix(path.suffix + ".checkpoint.jsonl")
    if checkpoint.exists():
        return load_checkpoint(checkpoint)
    return load_markdown_rows(path)


def filter_question_range(
    rows: list[dict[str, Any]],
    start_question: int | None,
    end_question: int | None,
) -> list[dict[str, Any]]:
    if start_question is None and end_question is None:
        return rows
    filtered: list[dict[str, Any]] = []
    for row in rows:
        try:
            number = int(row.get("number", ""))
        except (TypeError, ValueError):
            continue
        if start_question is not None and number < start_question:
            continue
        if end_question is not None and number > end_question:
            continue
        filtered.append(row)
    return filtered


def load_checkpoint(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_markdown_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = []
    for match in re.finditer(r"\n## Q(\d+): ([^\n]+)\n(.*?)(?=\n## Q\d+:|\Z)", text, re.DOTALL):
        body = match.group(3)
        rag_match = re.search(r"### RAG answer\n\n(.*?)(?:\n---|\Z)", body, re.DOTALL)
        normalized_match = re.search(r"- Normalized retrieval query: (.*)", body)
        rows.append(
            {
                "number": match.group(1),
                "question": match.group(2),
                "normalized_query": normalized_match.group(1).strip() if normalized_match else match.group(2),
                "rag_answer": rag_match.group(1).strip() if rag_match else "",
            }
        )
    return rows


def evidence_from_row(
    row: dict[str, Any],
    nodes: list[dict[str, Any]],
    node_lookup: dict[str, dict[str, Any]],
    index: Any,
    top_k: int,
    exclude_section_types: list[str],
    include_content_types: list[str] | None,
    exclude_content_types: list[str] | None,
) -> list[dict[str, Any]]:
    existing = row.get("evidence") or row.get("retrieved_evidence") or []
    if existing:
        return [
            {
                **item,
                "full_text": node_lookup.get(str(item.get("node_id")), {}).get("text") or item.get("full_text") or item.get("quote") or "",
            }
            for item in existing
        ]

    query = row.get("normalized_query") or row.get("question") or ""
    _, _, results = hybrid_search(
        nodes=nodes,
        index=index,
        query=query,
        top_k=top_k,
        exclude_section_types=exclude_section_types,
        include_content_types=include_content_types,
        exclude_content_types=exclude_content_types,
        bm25_weight=DEFAULT_BM25_WEIGHT,
        embedding_weight=DEFAULT_EMBEDDING_WEIGHT,
        penalize_single_source=True,
        bm25_only_penalty=DEFAULT_BM25_ONLY_PENALTY,
        embedding_only_penalty=DEFAULT_EMBEDDING_ONLY_PENALTY,
    )
    evidence: list[dict[str, Any]] = []
    for result in results:
        node_id = str(result.get("node_id"))
        node = node_lookup.get(node_id, {})
        evidence.append(
            {
                "node_id": node_id,
                "score": result.get("hybrid_score", result.get("score")),
                "title": result.get("title") or node.get("title"),
                "section_title": result.get("section_title") or node.get("section_title"),
                "full_text": node.get("text") or result.get("preview") or "",
            }
        )
    return evidence


def judge_claims(
    claims: list[Claim],
    node_texts: dict[str, str],
    retrieved_ids: set[str],
    model: str,
    max_context_chars: int,
) -> None:
    from openai import OpenAI

    client = OpenAI(timeout=DEFAULT_OPENAI_TIMEOUT_SECONDS)
    for claim in claims:
        if not claim.substantive:
            continue
        reference_ids = claim.cited_ids if claim.cited_ids else sorted(retrieved_ids)
        passages = [
            f"[{node_id}] {node_texts[node_id][:max_context_chars]}"
            for node_id in reference_ids
            if node_id in node_texts
        ][:8]
        if not passages:
            claim.faithfulness = "unsupported"
            claim.rationale = "No retrieved source text available."
            continue
        input_text = (
            "Return JSON only.\n\n"
            + "CLAIM:\n"
            + claim.text
            + "\n\nSOURCE:\n"
            + "\n\n".join(passages)
        )
        response = client.responses.create(
            model=model,
            instructions=JUDGE_SYSTEM,
            input=input_text,
            text={"format": {"type": "json_object"}},
            store=False,
            **openai_reasoning_kwargs(model),
        )
        try:
            payload = json.loads(response.output_text)
        except json.JSONDecodeError:
            claim.faithfulness = "unsupported"
            claim.rationale = "Judge returned invalid JSON."
            continue
        label = str(payload.get("label", "unsupported")).lower()
        if label not in {"supported", "unsupported", "contradicted"}:
            label = "unsupported"
        claim.faithfulness = label
        claim.rationale = str(payload.get("rationale", ""))


def write_markdown(results: list[dict[str, Any]], output_path: Path, judge_enabled: bool) -> None:
    total_substantive = sum(item["report"]["n_substantive"] for item in results)
    total_bare = sum(item["report"]["n_bare"] for item in results)
    total_unsupported = sum(item["report"]["n_unsupported"] for item in results)
    total_contradicted = sum(item["report"]["n_contradicted"] for item in results)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out:
        out.write("# RAG Faithfulness Check\n\n")
        out.write(f"- Questions: {len(results)}\n")
        out.write(f"- LLM judge: {'enabled' if judge_enabled else 'disabled'}\n")
        out.write(f"- Substantive claims: {total_substantive}\n")
        out.write(f"- Bare substantive claims: {total_bare}\n")
        if judge_enabled:
            out.write(f"- Unsupported claims: {total_unsupported}\n")
            out.write(f"- Contradicted claims: {total_contradicted}\n")
        out.write("\n")
        for item in results:
            report = item["report"]
            out.write(f"## Q{item['number']}: {item['question']}\n\n")
            out.write(f"- Substantive claims: {report['n_substantive']}\n")
            out.write(f"- Cited substantive claims: {report['n_cited']}\n")
            out.write(f"- Bare substantive claims: {report['n_bare']}\n")
            if judge_enabled:
                out.write(f"- Supported: {report['n_supported']}\n")
                out.write(f"- Unsupported: {report['n_unsupported']}\n")
                out.write(f"- Contradicted: {report['n_contradicted']}\n")
            out.write("\n")
            for claim in item["claims"]:
                if claim.get("bare") or claim.get("faithfulness") in {"unsupported", "contradicted"}:
                    out.write(f"- `{claim.get('faithfulness') or 'bare'}` {claim['text']}\n")
                    if claim.get("rationale"):
                        out.write(f"  Rationale: {claim['rationale']}\n")
            out.write("\n---\n\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Claim-level faithfulness check for RAG benchmark answers.")
    parser.add_argument("eval_path", type=Path, help="Eval markdown or checkpoint JSONL.")
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_PATH)
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE_DIR)
    parser.add_argument("--embedding-model", default=DEFAULT_MODEL)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--exclude-section-type", action="append", default=DEFAULT_EXCLUDED_SECTION_TYPES.copy())
    parser.add_argument("--include-content-type", action="append", default=[])
    parser.add_argument("--exclude-content-type", action="append", default=[])
    parser.add_argument("--max-context-chars", type=int, default=MAX_CONTEXT_CHARS_PER_NODE)
    parser.add_argument("--judge", action="store_true", help="Run LLM judge against retrieved chunks.")
    parser.add_argument("--model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--start-question", type=int, default=None)
    parser.add_argument("--end-question", type=int, default=None)
    parser.add_argument(
        "--prefer-markdown",
        action="store_true",
        help="Parse eval markdown directly even if a checkpoint file exists.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_PATH)
    args = parser.parse_args()

    all_nodes = load_nodes(args.nodes)
    filtered_nodes = filter_nodes(
        all_nodes,
        args.exclude_section_type,
        args.include_content_type,
        args.exclude_content_type,
    )
    node_lookup = {str(node["node_id"]): node for node in all_nodes}
    index = load_index(args.storage_dir, args.embedding_model)
    rows = filter_question_range(
        load_eval_rows(args.eval_path, prefer_markdown=args.prefer_markdown),
        args.start_question,
        args.end_question,
    )

    results: list[dict[str, Any]] = []
    for row in rows:
        evidence = evidence_from_row(
            row=row,
            nodes=filtered_nodes,
            node_lookup=node_lookup,
            index=index,
            top_k=args.top_k,
            exclude_section_types=args.exclude_section_type,
            include_content_types=args.include_content_type,
            exclude_content_types=args.exclude_content_type,
        )
        allowed_ids = {str(item["node_id"]) for item in evidence if item.get("node_id")}
        node_texts = {
            str(item["node_id"]): str(item.get("full_text") or "")
            for item in evidence
            if item.get("node_id")
        }
        answer = sanitize_answer_citations(str(row.get("rag_answer", "")), evidence)
        claims = extract_claims(answer)
        if args.judge:
            judge_claims(claims, node_texts, allowed_ids, args.model, args.max_context_chars)
        answer_report = report_claims(claims)
        result = {
            "number": str(row.get("number", "")),
            "question": str(row.get("question", "")),
            "normalized_query": str(row.get("normalized_query", row.get("question", ""))),
            "retrieved_node_ids": sorted(allowed_ids),
            "report": asdict(answer_report),
            "claims": [asdict(claim) for claim in claims],
        }
        results.append(result)
        print(
            f"Q{result['number']}: claims={answer_report.n_claims} "
            f"substantive={answer_report.n_substantive} cited={answer_report.n_cited} "
            f"bare={answer_report.n_bare}",
            flush=True,
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(results, args.markdown_out, args.judge)
    print(f"Wrote {args.out}", flush=True)
    print(f"Wrote {args.markdown_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
