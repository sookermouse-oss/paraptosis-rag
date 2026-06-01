from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from src.literature_store import DEFAULT_LITERATURE_DIR, LiteratureStore, clean_text, encode_json


PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
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
            "Unified literature metadata enrichment. PubMed mode uses PMID and EFetch XML to add "
            "MeSH terms, publication types, journal info, and grants. OpenAlex mode enriches existing "
            "papers by DOI first, then PMID, adding OpenAlex ID, citation count, OA status, type, "
            "publication dates, and concepts. Writes papers/source_records only; does not download "
            "full text and does not create content assets."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", choices=["pubmed", "openalex", "all"], required=True)
    parser.add_argument("--limit", type=int, default=None, help="Maximum papers to attempt per selected source.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_LITERATURE_DIR)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--mailto", help="Email passed to OpenAlex polite pool. Required for --source openalex/all.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.source in {"openalex", "all"} and not args.mailto:
        raise SystemExit("--mailto is required for --source openalex or --source all.")

    store = LiteratureStore(args.data_dir)
    if args.source in {"pubmed", "all"}:
        enrich_pubmed(store, args.limit, args.timeout)
    if args.source in {"openalex", "all"}:
        enrich_openalex(store, args.limit, args.timeout, args.mailto or "")
    store.save()
    print(f"Wrote literature tables to {args.data_dir}", flush=True)
    return 0


def enrich_pubmed(store: LiteratureStore, limit: int | None, timeout: int) -> None:
    attempted = 0
    enriched = 0
    for paper in list(store.papers):
        pmid = paper.get("pmid", "")
        if not pmid:
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1

        root = fetch_pubmed_xml(pmid, timeout)
        if root is None:
            print_progress("PubMed", attempted, enriched, "PMID", pmid)
            continue
        metadata = extract_pubmed_metadata(root, pmid)
        if not metadata:
            print_progress("PubMed", attempted, enriched, "PMID", pmid)
            continue

        updated_paper = store.upsert_paper({**paper, **metadata})
        store.upsert_source_record(
            {
                "paper_id": updated_paper["paper_id"],
                "source": "pubmed",
                "source_id": metadata.get("pmid", pmid),
                "doi": updated_paper.get("doi", ""),
                "pmid": metadata.get("pmid", pmid),
                "pmcid": updated_paper.get("pmcid", ""),
                "title": updated_paper.get("title", ""),
                "publication_date": updated_paper.get("publication_date", ""),
                "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{metadata.get('pmid', pmid)}/",
                "raw_json": encode_json(metadata),
            }
        )
        enriched += 1
        print_progress("PubMed", attempted, enriched, "PMID", pmid)

    print(f"PubMed papers attempted: {attempted}", flush=True)
    print(f"PubMed papers enriched: {enriched}", flush=True)


def enrich_openalex(store: LiteratureStore, limit: int | None, timeout: int, mailto: str) -> None:
    attempted = 0
    enriched = 0
    for paper in list(store.papers):
        if not paper.get("doi") and not paper.get("pmid"):
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        current_id = paper.get("doi") or paper.get("pmid") or paper.get("paper_id", "")

        response_json = fetch_openalex_work(paper, timeout, mailto)
        if not response_json:
            print_progress("OpenAlex", attempted, enriched, "DOI/PMID", current_id)
            continue

        metadata = openalex_metadata(response_json)
        updated_paper = store.upsert_paper({**paper, **metadata})
        openalex_id = metadata.get("openalex_id", "")
        store.upsert_source_record(
            {
                "paper_id": updated_paper["paper_id"],
                "source": "openalex",
                "source_id": openalex_id,
                "doi": updated_paper.get("doi", ""),
                "pmid": updated_paper.get("pmid", ""),
                "pmcid": updated_paper.get("pmcid", ""),
                "title": updated_paper.get("title", ""),
                "publication_date": metadata.get("publication_date", ""),
                "source_url": openalex_id,
                "raw_json": encode_json(response_json),
            }
        )
        enriched += 1
        print_progress("OpenAlex", attempted, enriched, "DOI/PMID", current_id)

    print(f"OpenAlex papers attempted: {attempted}", flush=True)
    print(f"OpenAlex papers enriched: {enriched}", flush=True)


def print_progress(source: str, attempted: int, enriched: int, current_label: str, current_id: str) -> None:
    if attempted % 10 == 0:
        print(f"{source} attempted {attempted}, enriched {enriched}, current {current_label} {current_id}", flush=True)


def fetch_pubmed_xml(pmid: str, timeout: int) -> ET.Element | None:
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.get(
                PUBMED_EFETCH_URL,
                params={"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "paraptosis_rag_metadata_enrichment"},
                timeout=timeout,
            )
            response.raise_for_status()
            return ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError) as exc:
            print(f"PubMed EFetch {pmid} attempt {attempt}/{REQUEST_RETRIES} failed: {exc}", file=sys.stderr)
    return None


def fetch_openalex_work(paper: dict[str, str], timeout: int, mailto: str) -> dict[str, Any] | None:
    doi = clean_text(paper.get("doi", ""))
    pmid = clean_text(paper.get("pmid", ""))
    if doi:
        url = f"{OPENALEX_WORKS_URL}/https://doi.org/{quote(doi, safe='/:')}"
        params = {"mailto": mailto}
    elif pmid:
        url = OPENALEX_WORKS_URL
        params = {"filter": f"ids.pmid:{pmid}", "mailto": mailto}
    else:
        return None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
            if "results" in payload:
                results = payload.get("results") or []
                return results[0] if results else None
            return payload
        except (requests.RequestException, ValueError) as exc:
            print(f"OpenAlex lookup attempt {attempt}/{REQUEST_RETRIES} failed for {doi or pmid}: {exc}", file=sys.stderr)
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
    grants = [
        {"grant_id": text_at(grant, "GrantID"), "agency": text_at(grant, "Agency"), "country": text_at(grant, "Country")}
        for grant in article.findall(".//Grant")
    ]
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


def openalex_metadata(work: dict[str, Any]) -> dict[str, Any]:
    open_access = work.get("open_access", {}) if isinstance(work.get("open_access"), dict) else {}
    return {
        "openalex_id": work.get("id", ""),
        "cited_by_count": work.get("cited_by_count", ""),
        "publication_year": work.get("publication_year", ""),
        "publication_date": work.get("publication_date", ""),
        "open_access_oa_status": open_access.get("oa_status", ""),
        "open_access_is_oa": open_access.get("is_oa", ""),
        "type": work.get("type", ""),
        "concepts": [
            {"id": concept.get("id", ""), "display_name": concept.get("display_name", ""), "level": concept.get("level", ""), "score": concept.get("score", "")}
            for concept in work.get("concepts", [])
            if isinstance(concept, dict)
        ],
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
