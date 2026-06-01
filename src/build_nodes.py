from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from parse_pmc_xml import parse_pmc_xml

try:
    from llama_index.core import Document
    from llama_index.core.schema import TextNode
except ImportError:  # Allows smoke tests before dependencies are installed.
    Document = None
    TextNode = None


DEFAULT_XML_DIR = Path("fulltext_xml")
DEFAULT_OUTPUT = Path("data/nodes.jsonl")
DEFAULT_METADATA_OUTPUT = Path("data/metadata.jsonl")
DEFAULT_LITERATURE_DIR = Path("data/literature")
DEFAULT_CONTENT_ASSETS_PATH = DEFAULT_LITERATURE_DIR / "content_assets.json"
DEFAULT_PAPERS_PATH = DEFAULT_LITERATURE_DIR / "papers.json"
CHUNK_SIZE_CHARS = 3600
CHUNK_OVERLAP_CHARS = 600
MIN_TEXT_CHARS = 100
LEADING_SECTION_NUMBER_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,3})*\.?\s+")
CITATION_EMPTY_BRACKETS_RE = re.compile(r"\[\s*(?:[,;]\s*)*\]")
CITATION_FIG_TABLE_RE = re.compile(r"\(\s*(?:Fig\.?|Figure|Table)\s*[A-Za-z0-9]*\s*\)", re.IGNORECASE)
CITATION_TRAILING_FIG_TABLE_RE = re.compile(
    r"\b(?:Suppl\.?|Supplementary|Supplemental)?\s*(?:Fig\.?|Figure|Table)\s*\)\.",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;:])\s+|\n+")
RESULTS_OVERRIDE_TERMS = (
    "oncogenic role",
    "triggered",
    "induced",
    "disrupted",
    "predicts",
)
SECTION_TYPE_TERMS = {
    "conclusion": ("conclusion", "conclusions"),
    "discussion": ("discussion", "future perspective"),
    "intro": ("introduction", "background"),
    "methods": (
        "method",
        "material",
        "microscopy",
        "transmission electron microscopy",
        "immune infiltration landscape",
        "tumor mutational analysis",
        "drug sensitivity analysis",
        "using scrna-seq",
        "cell culture",
        "western blot",
        "western blotting",
        "flow cytometry",
        "statistical",
        "statistical analysis",
        "assay",
        "transwell",
        "wound healing",
        "data collection",
        "sequencing",
        "analysis",
        "validation",
        "nomogram",
        "model",
        "signature",
        "cohort",
        "database",
    ),
    "results": (
        "result",
        "finding",
        "identification",
        "identified",
        "construction and validation",
        "construction and validation of prrs",
        "functional enrichment analysis",
        "predicts",
        "prrs predicts",
        "profiling reveals",
        "disrupted",
        "triggered",
        "enhanced",
        "induced",
        "identifying",
        "oncogenic role",
    ),
}
REVIEW_SECTION_TITLES = {
    "paraptosis",
    "ferroptosis",
    "necroptosis",
    "pyroptosis",
    "autophagic cell death",
}


def build_nodes(
    xml_dir: Path = DEFAULT_XML_DIR,
    output_path: Path = DEFAULT_OUTPUT,
    metadata_output_path: Path = DEFAULT_METADATA_OUTPUT,
    limit: int | None = None,
    content_assets_path: Path = DEFAULT_CONTENT_ASSETS_PATH,
    papers_path: Path = DEFAULT_PAPERS_PATH,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    xml_files = sorted(xml_dir.glob("*.xml"))
    if limit is not None:
        xml_files = xml_files[:limit]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_output_path.parent.mkdir(parents=True, exist_ok=True)
    node_dicts: list[dict[str, Any]] = []
    metadata_dicts: list[dict[str, Any]] = []
    article_count = 0
    seen_node_ids: set[str] = set()

    for xml_file in xml_files:
        article = parse_pmc_xml(xml_file)
        article_count += 1
        metadata_dicts.append(_article_metadata(article))
        _make_document(article)

        for section in article["body_sections"]:
            section_text = clean_citation_residue(clean_leading_section_number(section["text"]))
            if len("".join(section_text.split())) < MIN_TEXT_CHARS:
                continue
            for chunk_index, chunk_text in enumerate(chunk_text_by_chars(section_text)):
                chunk_text = clean_citation_residue(clean_leading_section_number(chunk_text))
                if len("".join(chunk_text.split())) < MIN_TEXT_CHARS:
                    continue
                node_dict = _node_dict(article, section, chunk_text, chunk_index)
                _make_text_node(node_dict)
                node_dicts.append(node_dict)
                seen_node_ids.add(node_dict["node_id"])

    for abstract_node in abstract_nodes(content_assets_path, papers_path):
        if abstract_node["node_id"] in seen_node_ids:
            continue
        _make_text_node(abstract_node)
        node_dicts.append(abstract_node)
        seen_node_ids.add(abstract_node["node_id"])

    with output_path.open("w", encoding="utf-8") as handle:
        for node in node_dicts:
            handle.write(json.dumps(node, ensure_ascii=False) + "\n")

    with metadata_output_path.open("w", encoding="utf-8") as handle:
        for metadata in metadata_dicts:
            handle.write(json.dumps(metadata, ensure_ascii=False) + "\n")

    return article_count, node_dicts, metadata_dicts


def chunk_text_by_chars(
    text: str,
    chunk_size: int = CHUNK_SIZE_CHARS,
    chunk_overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    sentences = split_sentences(text)
    chunks: list[str] = []
    current: list[str] = []

    for sentence in sentences:
        if len(sentence) > chunk_size:
            if current:
                chunks.append(" ".join(current).strip())
                current = _overlap_sentences(current, chunk_overlap)
            chunks.extend(_hard_chunk_long_sentence(sentence, chunk_size, chunk_overlap))
            continue

        candidate = " ".join([*current, sentence]).strip()
        if current and len(candidate) > chunk_size:
            chunks.append(" ".join(current).strip())
            current = _overlap_sentences(current, chunk_overlap)
        current.append(sentence)

    if current:
        chunks.append(" ".join(current).strip())
    return chunks


def split_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in SENTENCE_SPLIT_RE.split(text) if sentence.strip()]


def clean_leading_section_number(text: str) -> str:
    return LEADING_SECTION_NUMBER_RE.sub("", text, count=1).strip()


def clean_citation_residue(text: str) -> str:
    text = CITATION_EMPTY_BRACKETS_RE.sub("", text)
    text = CITATION_FIG_TABLE_RE.sub("", text)
    text = CITATION_TRAILING_FIG_TABLE_RE.sub("", text)
    text = re.sub(r"\b[Ss]upporting [Ii]nformation\b", "supplement", text)
    text = re.sub(r"\b[Rr]eferences\b", "literature", text)
    text = re.sub(r"\bet\s+al\.\.", "et al.", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+([,;:.!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _overlap_sentences(sentences: list[str], overlap_chars: int) -> list[str]:
    overlap: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        sentence_len = len(sentence) + (1 if overlap else 0)
        if overlap and total + sentence_len > overlap_chars:
            break
        overlap.insert(0, sentence)
        total += sentence_len
    return overlap


def _hard_chunk_long_sentence(sentence: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(sentence):
        end = min(start + chunk_size, len(sentence))
        if end < len(sentence):
            boundary = sentence.rfind(" ", start, end)
            if boundary > start + int(chunk_size * 0.65):
                end = boundary
        chunk = sentence[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(sentence):
            break
        start = max(end - chunk_overlap, start + 1)
        if start < len(sentence) and start > 0 and not sentence[start - 1].isspace():
            next_space = sentence.find(" ", start)
            if next_space != -1:
                start = next_space + 1
    return chunks


def _node_dict(
    article: dict[str, Any],
    section: dict[str, Any],
    text: str,
    chunk_index: int,
) -> dict[str, Any]:
    pmcid = article.get("pmcid") or Path(article["source_file"]).stem
    section_index = section["section_index"]
    node_id = f"{pmcid}:sec{section_index}:chunk{chunk_index}"
    return {
        "node_id": node_id,
        "text": text,
        "pmcid": article.get("pmcid"),
        "pmid": article.get("pmid"),
        "doi": article.get("doi"),
        "title": article.get("title"),
        "year": article.get("year"),
        "journal": article.get("journal"),
        "keywords": article.get("keywords", []),
        "content_type": "fulltext",
        "section_title": section["section_title"],
        "section_type": infer_section_type(section["section_title"]),
        "section_index": section_index,
        "chunk_index": chunk_index,
        "source_file": article["source_file"],
    }


def abstract_nodes(content_assets_path: Path, papers_path: Path) -> list[dict[str, Any]]:
    papers = load_json_rows(papers_path)
    paper_by_id = {paper.get("paper_id"): paper for paper in papers if paper.get("paper_id")}
    best_abstract_by_paper_id: dict[str, dict[str, Any]] = {}
    for asset in load_json_rows(content_assets_path):
        if asset.get("content_type") != "abstract":
            continue
        paper_id = asset.get("paper_id", "")
        text = clean_citation_residue(asset.get("text", ""))
        if not paper_id or len("".join(text.split())) < MIN_TEXT_CHARS:
            continue
        existing = best_abstract_by_paper_id.get(paper_id)
        if existing is None or len(text) > len(existing.get("text", "")):
            best_abstract_by_paper_id[paper_id] = {**asset, "text": text}

    nodes: list[dict[str, Any]] = []
    for paper_id, asset in sorted(best_abstract_by_paper_id.items()):
        paper = paper_by_id.get(paper_id, {})
        identifier = paper.get("pmcid") or paper_id
        node = {
            "node_id": f"{identifier}:abstract:chunk0",
            "text": asset["text"],
            "paper_id": paper_id,
            "pmcid": paper.get("pmcid") or None,
            "pmid": paper.get("pmid") or None,
            "doi": paper.get("doi") or None,
            "title": paper.get("title") or "",
            "year": paper.get("year") or paper.get("publication_year") or "",
            "journal": paper.get("journal_title") or paper.get("journal_iso") or "",
            "keywords": [],
            "content_type": "abstract",
            "section_title": "Abstract",
            "section_type": "abstract",
            "section_index": -1,
            "chunk_index": 0,
            "source_file": content_assets_path.as_posix(),
        }
        nodes.append(node)
    return nodes


def load_json_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def infer_section_type(section_title: str) -> str:
    title = section_title.casefold()
    if _contains_any(title, SECTION_TYPE_TERMS["conclusion"]):
        return "conclusion"
    if _contains_any(title, SECTION_TYPE_TERMS["discussion"]):
        return "discussion"
    if _contains_any(title, SECTION_TYPE_TERMS["intro"]):
        return "intro"
    if _contains_any(title, RESULTS_OVERRIDE_TERMS) or _contains_any(title, SECTION_TYPE_TERMS["results"]):
        return "results"
    if _contains_any(title, SECTION_TYPE_TERMS["methods"]):
        return "methods"
    if _is_review_section(title):
        return "review"
    return "other"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _is_review_section(title: str) -> bool:
    return title in REVIEW_SECTION_TITLES or title.startswith("non-apoptotic rcds")


def _article_metadata(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "pmcid": article.get("pmcid"),
        "pmid": article.get("pmid"),
        "doi": article.get("doi"),
        "title": article.get("title"),
        "year": article.get("year"),
        "journal": article.get("journal"),
        "authors": article.get("authors", []),
        "keywords": article.get("keywords", []),
        "abstract": article.get("abstract", ""),
        "source_file": article["source_file"],
    }


def _make_document(article: dict[str, Any]) -> Any:
    if Document is None:
        return None
    text_parts = [article.get("abstract") or ""]
    text_parts.extend(section["text"] for section in article.get("body_sections", []))
    return Document(text="\n\n".join(part for part in text_parts if part), metadata=_metadata(article))


def _make_text_node(node_dict: dict[str, Any]) -> Any:
    if TextNode is None:
        return None
    metadata = {key: value for key, value in node_dict.items() if key not in {"node_id", "text"}}
    return TextNode(id_=node_dict["node_id"], text=node_dict["text"], metadata=metadata)


def _metadata(article: dict[str, Any]) -> dict[str, Any]:
    return {
        key: article.get(key)
        for key in (
            "pmcid",
            "pmid",
            "doi",
            "title",
            "journal",
            "year",
            "authors",
            "keywords",
            "source_file",
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build ingestion nodes from PMC full-text XML files.")
    parser.add_argument("--xml-dir", type=Path, default=DEFAULT_XML_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metadata-output", type=Path, default=DEFAULT_METADATA_OUTPUT)
    parser.add_argument("--content-assets", type=Path, default=DEFAULT_CONTENT_ASSETS_PATH)
    parser.add_argument("--papers", type=Path, default=DEFAULT_PAPERS_PATH)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    article_count, nodes, metadata = build_nodes(
        args.xml_dir,
        args.output,
        args.metadata_output,
        args.limit,
        args.content_assets,
        args.papers,
    )
    print(f"Parsed articles: {article_count}")
    print(f"Generated nodes: {len(nodes)}")
    content_type_counts: dict[str, int] = {}
    for node in nodes:
        content_type = node.get("content_type") or "missing"
        content_type_counts[content_type] = content_type_counts.get(content_type, 0) + 1
    print(f"Content type distribution: {content_type_counts}")
    print(f"Wrote metadata records: {len(metadata)}")
    print("First 2 nodes:")
    for node in nodes[:2]:
        print(json.dumps(node, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
