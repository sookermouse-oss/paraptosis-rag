from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


SKIP_TEXT_TAGS = {
    "caption",
    "table",
    "table-wrap",
    "fig",
    "graphic",
    "media",
    "supplementary-material",
    "xref",
    "ref-list",
    "back",
}

SKIP_SECTION_TITLES = {
    "abbreviations",
    "acknowledgments",
    "author contributions",
    "author contribution",
    "author information",
    "authors' contribution",
    "authors’ contribution",
    "availability of data and materials",
    "competing interests",
    "conflict of interest statement",
    "conflict of interest",
    "conflicts of interest",
    "consent for publication",
    "credit authorship contribution statement",
    "data and code availability",
    "data availability",
    "ethics statement",
    "ethical statement",
    "declarations",
    "declaration of competing interest",
    "supporting information",
    "supplementary information",
    "supplementary material",
    "declaration of generative ai",
    "data availability statement",
    "acknowledgements",
    "funding",
    "funding information",
    "informed consent statement",
    "institutional review board statement",
    "lead contact",
    "references",
    "resource availability",
}


def parse_pmc_xml(path: str | Path) -> dict[str, Any]:
    """Parse a PMC/JATS XML file into article metadata and body sections."""
    source_path = Path(path)
    root = ET.parse(source_path).getroot()
    article = _first(root, "article")
    if article is None:
        raise ValueError(f"No <article> element found in {source_path}")

    front = _first(article, "front")
    article_meta = _first(front, "article-meta") if front is not None else None
    journal_meta = _first(front, "journal-meta") if front is not None else None
    body = _first(article, "body")

    metadata = {
        "pmcid": _article_id(article_meta, "pmcid"),
        "pmid": _article_id(article_meta, "pmid"),
        "doi": _article_id(article_meta, "doi"),
        "title": _clean_text(_text_from_first(article_meta, "article-title")),
        "journal": _clean_text(_text_from_first(journal_meta, "journal-title"))
        or _clean_text(_text_from_first(journal_meta, "journal-id")),
        "year": _publication_year(article_meta),
        "authors": _authors(article_meta),
        "keywords": _keywords(article_meta),
        "abstract": _abstract(article_meta),
        "source_file": str(source_path),
    }

    return {
        **metadata,
        "body_sections": _body_sections(body),
    }


def _body_sections(body: ET.Element | None) -> list[dict[str, Any]]:
    if body is None:
        return []

    sections: list[dict[str, Any]] = []
    for index, sec in enumerate(_children(body, "sec")):
        _collect_sections(sec, sections, index_hint=index)
    return sections


def _collect_sections(
    sec: ET.Element,
    sections: list[dict[str, Any]],
    index_hint: int | None = None,
    parent_title: str | None = None,
) -> None:
    title = _clean_text(_text_from_first(sec, "title")) or parent_title or "Untitled section"
    if _should_skip_section(title):
        return

    text_parts: list[str] = []

    for child in list(sec):
        tag = _local_name(child.tag)
        if tag == "title" or tag == "sec":
            continue
        if tag in SKIP_TEXT_TAGS:
            continue
        child_text = _clean_text(_iter_text_excluding(child, SKIP_TEXT_TAGS))
        if child_text:
            text_parts.append(child_text)

    section_text = "\n\n".join(text_parts)
    if section_text and not section_text.casefold().startswith("graphical abstract"):
        sections.append(
            {
                "section_title": title,
                "section_index": len(sections) if index_hint is None else len(sections),
                "text": section_text,
            }
        )

    for child in _children(sec, "sec"):
        _collect_sections(child, sections, parent_title=title)


def _article_id(article_meta: ET.Element | None, id_type: str) -> str | None:
    if article_meta is None:
        return None
    for element in _descendants(article_meta, "article-id"):
        if element.attrib.get("pub-id-type") == id_type:
            return _clean_text(_iter_text(element)) or None
    return None


def _publication_year(article_meta: ET.Element | None) -> str | None:
    if article_meta is None:
        return None

    preferred_types = ("epub", "ppub", "collection")
    pub_dates = list(_descendants(article_meta, "pub-date"))
    for pub_type in preferred_types:
        for pub_date in pub_dates:
            if pub_date.attrib.get("pub-type") == pub_type:
                year = _clean_text(_text_from_first(pub_date, "year"))
                if year:
                    return year

    for pub_date in pub_dates:
        year = _clean_text(_text_from_first(pub_date, "year"))
        if year:
            return year
    return None


def _authors(article_meta: ET.Element | None) -> list[str]:
    if article_meta is None:
        return []

    authors: list[str] = []
    for contrib in _descendants(article_meta, "contrib"):
        if contrib.attrib.get("contrib-type") != "author":
            continue
        name = _first(contrib, "name")
        collab = _first(contrib, "collab")
        if name is not None:
            given = _clean_text(_text_from_first(name, "given-names"))
            surname = _clean_text(_text_from_first(name, "surname"))
            full_name = " ".join(part for part in (given, surname) if part)
            if full_name:
                authors.append(full_name)
        elif collab is not None:
            collab_name = _clean_text(_iter_text(collab))
            if collab_name:
                authors.append(collab_name)
    return authors


def _keywords(article_meta: ET.Element | None) -> list[str]:
    if article_meta is None:
        return []
    return [
        keyword
        for keyword in (_clean_text(_iter_text(element)) for element in _descendants(article_meta, "kwd"))
        if keyword
    ]


def _abstract(article_meta: ET.Element | None) -> str:
    if article_meta is None:
        return ""
    abstracts = []
    for element in _children(article_meta, "abstract"):
        if element.attrib.get("abstract-type"):
            continue
        text = _clean_text(_iter_text(element))
        if text:
            abstracts.append(text)
    return _strip_abstract_label("\n\n".join(abstracts))


def _first(element: ET.Element | None, tag_name: str) -> ET.Element | None:
    if element is None:
        return None
    for candidate in element.iter():
        if _local_name(candidate.tag) == tag_name:
            return candidate
    return None


def _children(element: ET.Element | None, tag_name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [child for child in list(element) if _local_name(child.tag) == tag_name]


def _descendants(element: ET.Element | None, tag_name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [candidate for candidate in element.iter() if _local_name(candidate.tag) == tag_name]


def _text_from_first(element: ET.Element | None, tag_name: str) -> str:
    child = _first(element, tag_name)
    return _iter_text(child) if child is not None else ""


def _iter_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    parts: list[str] = []
    for text in element.itertext():
        if text:
            parts.append(text)
    return " ".join(parts)


def _iter_text_excluding(element: ET.Element | None, excluded_tags: set[str]) -> str:
    if element is None or _local_name(element.tag) in excluded_tags:
        return ""

    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in list(element):
        if _local_name(child.tag) not in excluded_tags:
            child_text = _iter_text_excluding(child, excluded_tags)
            if child_text:
                parts.append(child_text)
        if child.tail:
            parts.append(child.tail)
    return " ".join(parts)


def _should_skip_section(title: str) -> bool:
    normalized = _normalize_title(title)
    return any(skip_title in normalized for skip_title in SKIP_SECTION_TITLES)


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.casefold()).strip()


def _strip_abstract_label(text: str) -> str:
    return re.sub(r"^Abstract\s+", "", text).strip()


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:The\s+)?graphical abstract[^.!?]*[.!?]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*(?:[,;]\s*)*\]", "", text)
    text = re.sub(r"\b[Ss]upporting [Ii]nformation\b", "supplement", text)
    text = re.sub(r"\b[Rr]eferences\b", "literature", text)
    text = re.sub(r"\(([A-Z][A-Za-z-]+\s+et\s+al\.)\s*\)", r"\1", text)
    text = re.sub(r"([A-Z][A-Za-z-]+\s+et\s+al\.)\s*\)", r"\1", text)
    text = re.sub(r"\bet\s+al\.\.", "et al.", text)
    text = re.sub(r"\(\s*(?:Fig\.?|Figure|Table)\s+[A-Za-z0-9]+\s*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s*(?:Fig\.?|Figure|Table)\s*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"([.!?])\s+(?:[,;]\s*)+", r"\1 ", text)
    text = re.sub(r"(?:^|\s)(?:[,;]\s*){2,}", " ", text)
    text = re.sub(
        r"\(\s*(?:(?:Figure|Fig\.?|Table)\s*[,;]?\s*|\b(?:and|or)\b\s*)+\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\(\s*(?:Figure|Fig\.?|Table)\s*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s*(?:and|or|,|;|\s)+\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+([,;:.!?])", r"\1", text)
    text = re.sub(r"\s+([)])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
