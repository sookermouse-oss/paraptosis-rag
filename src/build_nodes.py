from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from parse_pmc_xml import parse_pmc_xml

try:
    from llama_index.core import Document
    from llama_index.core.schema import TextNode
except ImportError:  # Allows smoke tests before dependencies are installed.
    Document = None
    TextNode = None


DEFAULT_XML_DIR = Path("/Users/shuangsu/Documents/Projects/paraptosis-biorxiv-job/fulltext_xml")
DEFAULT_OUTPUT = Path("data/nodes.jsonl")
CHUNK_SIZE_CHARS = 3600
CHUNK_OVERLAP_CHARS = 600
MIN_TEXT_CHARS = 100


def build_nodes(
    xml_dir: Path = DEFAULT_XML_DIR,
    output_path: Path = DEFAULT_OUTPUT,
    limit: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    xml_files = sorted(xml_dir.glob("*.xml"))
    if limit is not None:
        xml_files = xml_files[:limit]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    node_dicts: list[dict[str, Any]] = []
    article_count = 0

    for xml_file in xml_files:
        article = parse_pmc_xml(xml_file)
        article_count += 1
        _make_document(article)

        for section in article["body_sections"]:
            if len("".join(section["text"].split())) < MIN_TEXT_CHARS:
                continue
            for chunk_index, chunk_text in enumerate(chunk_text_by_chars(section["text"])):
                if len("".join(chunk_text.split())) < MIN_TEXT_CHARS:
                    continue
                node_dict = _node_dict(article, section, chunk_text, chunk_index)
                _make_text_node(node_dict)
                node_dicts.append(node_dict)

    with output_path.open("w", encoding="utf-8") as handle:
        for node in node_dicts:
            handle.write(json.dumps(node, ensure_ascii=False) + "\n")

    return article_count, node_dicts


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

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            boundary = max(text.rfind(". ", start, end), text.rfind(" ", start, end))
            if boundary > start + int(chunk_size * 0.65):
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - chunk_overlap, start + 1)
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
        "authors": article.get("authors", []),
        "keywords": article.get("keywords", []),
        "abstract": article.get("abstract", ""),
        "section_title": section["section_title"],
        "section_index": section_index,
        "chunk_index": chunk_index,
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
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    article_count, nodes = build_nodes(args.xml_dir, args.output, args.limit)
    print(f"Parsed articles: {article_count}")
    print(f"Generated nodes: {len(nodes)}")
    print("First 2 nodes:")
    for node in nodes[:2]:
        print(json.dumps(node, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
