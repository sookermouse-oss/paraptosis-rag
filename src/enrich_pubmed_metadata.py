from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

from src.literature_store import DEFAULT_LITERATURE_DIR, LiteratureStore, clean_text, encode_json


PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
DEFAULT_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
MONTHS = {
    "jan": "01",
    "feb": "02",
    "mar": "03",
    "apr": "04",
    "may": "05",
    "jun": "06",
    "jul": "07",
    "aug": "08",
    "sep": "09",
    "oct": "10",
    "nov": "11",
    "dec": "12",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich existing canonical papers with PubMed EFetch XML metadata. For papers with PMID, "
            "adds MeSH terms, publication types, journal info, and grants when available. Updates "
            "papers.csv/json and source_records.csv/json. Does not fetch full text and does not "
            "create content assets."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_LITERATURE_DIR, help="Directory containing papers/source_records/content_assets tables.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Request timeout in seconds.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum papers to attempt.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = LiteratureStore(args.data_dir)
    attempted = 0
    enriched = 0

    for paper in store.papers:
        pmid = paper.get("pmid", "")
        if not pmid:
            continue
        if args.limit is not None and attempted >= args.limit:
            break
        attempted += 1

        root = fetch_pubmed_xml(pmid, args.timeout)
        if root is None:
            continue
        metadata = extract_pubmed_metadata(root, pmid)
        if not metadata:
            continue

        updated_paper = store.upsert_paper({"paper_id": paper["paper_id"], **paper, **metadata})
        store.upsert_source_record(
            {
                "paper_id": updated_paper["paper_id"],
                "source": "pubmed",
                "source_id": pmid,
                "doi": updated_paper.get("doi", ""),
                "pmid": pmid,
                "pmcid": updated_paper.get("pmcid", ""),
                "title": updated_paper.get("title", ""),
                "publication_date": updated_paper.get("publication_date", ""),
                "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "raw_json": encode_json(metadata),
            }
        )
        enriched += 1

    store.save()
    print(f"PubMed papers attempted: {attempted}")
    print(f"PubMed papers enriched: {enriched}")
    print(f"Wrote literature tables to {args.data_dir}")
    return 0


def fetch_pubmed_xml(pmid: str, timeout: int) -> ET.Element | None:
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.get(
                PUBMED_EFETCH_URL,
                params={
                    "db": "pubmed",
                    "id": pmid,
                    "retmode": "xml",
                    "tool": "paraptosis_rag_pubmed_enrichment",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            return ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError) as exc:
            print(f"PubMed EFetch {pmid} attempt {attempt}/{REQUEST_RETRIES} failed: {exc}", file=sys.stderr)
    return None


def extract_pubmed_metadata(root: ET.Element, fallback_pmid: str) -> dict[str, Any]:
    article = root.find(".//PubmedArticle")
    if article is None:
        return {}

    doi = ""
    pmcid = ""
    for article_id in article.findall("./PubmedData/ArticleIdList/ArticleId"):
        id_type = article_id.attrib.get("IdType", "").lower()
        value = clean_text(article_id.text or "")
        if id_type == "doi":
            doi = value
        elif id_type == "pmc":
            pmcid = value

    journal = article.find(".//Journal")
    grants = []
    for grant in article.findall(".//Grant"):
        grants.append(
            {
                "grant_id": text_at(grant, "GrantID"),
                "agency": text_at(grant, "Agency"),
                "country": text_at(grant, "Country"),
            }
        )

    return {
        "doi": doi,
        "pmid": text_at(article, ".//PMID") or fallback_pmid,
        "pmcid": pmcid,
        "title": text_at(article, ".//ArticleTitle"),
        "journal_title": text_at(journal, "Title") if journal is not None else "",
        "journal_iso": text_at(journal, "ISOAbbreviation") if journal is not None else "",
        "issn": text_at(journal, "ISSN") if journal is not None else "",
        "publication_date": pubmed_date(article),
        "mesh_terms": [clean_text("".join(heading.itertext())) for heading in article.findall(".//MeshHeading/DescriptorName")],
        "publication_types": [clean_text("".join(item.itertext())) for item in article.findall(".//PublicationType")],
        "grants": grants,
    }


def text_at(root: ET.Element | None, path: str) -> str:
    if root is None:
        return ""
    element = root.find(path)
    if element is None:
        return ""
    return clean_text("".join(element.itertext()))


def pubmed_date(article: ET.Element) -> str:
    year = text_at(article, ".//PubDate/Year")
    month = normalize_month(text_at(article, ".//PubDate/Month"))
    day = normalize_day(text_at(article, ".//PubDate/Day"))
    if year and month and day:
        return f"{year}-{month}-{day}"
    if year and month:
        return f"{year}-{month}"
    return year


def normalize_month(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    if value.isdigit():
        return value.zfill(2)
    return MONTHS.get(value[:3].lower(), value)


def normalize_day(value: str) -> str:
    value = clean_text(value)
    return value.zfill(2) if value.isdigit() else value


if __name__ == "__main__":
    raise SystemExit(main())
