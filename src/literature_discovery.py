from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

from src.literature_store import (
    DEFAULT_LITERATURE_DIR,
    LiteratureStore,
    clean_text,
    encode_json,
    normalize_pmcid,
)


EUROPE_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPE_PMC_REST_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DEFAULT_KEYWORDS: list[str] = []
DEFAULT_DAYS_BACK = 1460
DEFAULT_SOURCE_PAGE_SIZE = 100
DEFAULT_MAX_RECORDS = 1000
DEFAULT_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover canonical papers from Europe PMC. Europe PMC is the main discovery source; "
            "when keywords are provided, they are searched in TITLE_ABS. Writes papers.csv/json, "
            "source_records.csv/json, and content_assets.csv/json. Adds abstract content assets "
            "when abstracts are available. Deduplicates papers by DOI, then PMID, then PMCID, "
            "then normalized title. Does not download full text or PDFs."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--keyword", action="append", default=[], help="Keyword to search in TITLE_ABS. Can be repeated.")
    parser.add_argument("--keywords", help='Comma-separated TITLE_ABS keywords, e.g. "paraptosis,methuosis".')
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK, help="Publication date window.")
    parser.add_argument("--source-page-size", type=int, default=DEFAULT_SOURCE_PAGE_SIZE, help="Europe PMC page size.")
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS, help="Maximum Europe PMC records to scan.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Request timeout in seconds.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_LITERATURE_DIR, help="Directory for papers/source_records/content_assets CSV and JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    keywords = parse_keywords(args)
    store = LiteratureStore(args.data_dir)

    scanned = 0
    added_assets = 0
    for record in iter_europe_pmc_records(args, keywords):
        scanned += 1
        paper = store.upsert_paper(paper_from_europe_pmc(record))
        source_record = store.upsert_source_record(source_record_from_europe_pmc(record, paper["paper_id"], keywords))
        abstract = clean_text(record.get("abstractText", ""))
        if abstract:
            store.upsert_content_asset(
                {
                    "paper_id": paper["paper_id"],
                    "source_record_id": source_record["source_record_id"],
                    "source": "europe_pmc",
                    "content_type": "abstract",
                    "status": "available",
                    "remote_url": source_record.get("source_url", ""),
                    "mime_type": "text/plain",
                    "text": abstract,
                }
            )
            added_assets += 1

    store.save()
    print(f"Europe PMC records scanned: {scanned}")
    print(f"Abstract assets added/updated: {added_assets}")
    print(f"Papers: {len(store.papers)}")
    print(f"Source records: {len(store.source_records)}")
    print(f"Content assets: {len(store.content_assets)}")
    print(f"Wrote literature tables to {args.data_dir}")
    return 0


def parse_keywords(args: argparse.Namespace) -> list[str]:
    values = []
    values.extend(args.keyword or [])
    if args.keywords:
        values.append(args.keywords)

    keywords: list[str] = []
    for value in values:
        for part in str(value).split(","):
            keyword = part.strip()
            if keyword and keyword not in keywords:
                keywords.append(keyword)
    return keywords


def iter_europe_pmc_records(args: argparse.Namespace, keywords: list[str]):
    query = build_europe_pmc_query(keywords, args.days_back)
    cursor_mark = "*"
    fetched_count = 0

    while fetched_count < args.max_records:
        page_size = min(args.source_page_size, args.max_records - fetched_count)
        payload = get_json(
            EUROPE_PMC_SEARCH_URL,
            args.timeout,
            "Europe PMC discovery",
            params={
                "query": query,
                "format": "json",
                "resulttype": "core",
                "pageSize": page_size,
                "cursorMark": cursor_mark,
            },
        )
        if payload is None:
            break

        results = payload.get("resultList", {}).get("result", [])
        if not results:
            break

        for record in results:
            yield record

        fetched_count += len(results)
        print(f"Europe PMC fetched {fetched_count} candidate(s)...", flush=True)
        next_cursor_mark = str(payload.get("nextCursorMark", ""))
        if not next_cursor_mark or next_cursor_mark == cursor_mark:
            break
        cursor_mark = next_cursor_mark


def build_europe_pmc_query(keywords: list[str], days_back: int) -> str:
    start_date = date.today() - timedelta(days=days_back)
    date_query = f"FIRST_PDATE:[{start_date.isoformat()} TO {date.today().isoformat()}]"
    if not keywords:
        return date_query
    terms = [f'"{keyword}"' if " " in keyword else keyword for keyword in keywords]
    return f"TITLE_ABS:({' OR '.join(terms)}) AND {date_query}"


def paper_from_europe_pmc(record: dict[str, Any]) -> dict[str, Any]:
    journal = record.get("journalInfo", {}) if isinstance(record.get("journalInfo"), dict) else {}
    journal_title = journal.get("journal", {}).get("title", "") if isinstance(journal.get("journal"), dict) else ""
    journal_iso = journal.get("journal", {}).get("isoabbreviation", "") if isinstance(journal.get("journal"), dict) else ""
    return {
        "doi": record.get("doi", ""),
        "pmid": record.get("pmid", ""),
        "pmcid": normalize_pmcid(record.get("pmcid", "")),
        "title": record.get("title", ""),
        "authors": record.get("authorString", ""),
        "journal_title": journal_title,
        "journal_iso": journal_iso,
        "issn": journal.get("journal", {}).get("issn", "") if isinstance(journal.get("journal"), dict) else "",
        "publication_date": europe_pmc_date(record),
    }


def source_record_from_europe_pmc(record: dict[str, Any], paper_id: str, keywords: list[str]) -> dict[str, Any]:
    source = clean_text(record.get("source", ""))
    source_id = clean_text(record.get("id", ""))
    pmcid = normalize_pmcid(record.get("pmcid", ""))
    source_url = f"https://europepmc.org/article/{source}/{source_id}" if source and source_id else ""
    raw_record = dict(record)
    if has_europe_pmc_fulltext_xml(record):
        raw_record["europe_pmc_fulltext_xml_url"] = f"{EUROPE_PMC_REST_BASE_URL}/{source}/{source_id}/fullTextXML"
    return {
        "paper_id": paper_id,
        "source": "europe_pmc",
        "source_id": f"{source}:{source_id}" if source and source_id else source_id,
        "doi": record.get("doi", ""),
        "pmid": record.get("pmid", ""),
        "pmcid": pmcid,
        "title": record.get("title", ""),
        "publication_date": europe_pmc_date(record),
        "source_url": source_url,
        "matched_keywords": match_keywords(record.get("title", ""), record.get("abstractText", ""), keywords),
        "raw_json": encode_json(raw_record),
    }


def europe_pmc_date(record: dict[str, Any]) -> str:
    for key in ["firstPublicationDate", "pubYear"]:
        if record.get(key):
            return str(record.get(key))
    journal_info = record.get("journalInfo", {})
    if isinstance(journal_info, dict):
        return str(journal_info.get("printPublicationDate") or journal_info.get("yearOfPublication") or "")
    return ""


def has_europe_pmc_fulltext_xml(record: dict[str, Any]) -> bool:
    source = clean_text(record.get("source", ""))
    source_id = clean_text(record.get("id", ""))
    if not source or not source_id:
        return False
    return source == "PMC" or clean_text(record.get("inEPMC", "")).upper() == "Y"


def match_keywords(title: Any, abstract: Any, keywords: list[str]) -> list[str]:
    haystack = f"{title or ''}\n{abstract or ''}"
    return [
        keyword
        for keyword in keywords
        if re.search(re.escape(keyword), haystack, flags=re.IGNORECASE)
    ]


def get_json(url: str, timeout: int, label: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            print(f"{label} attempt {attempt}/{REQUEST_RETRIES} failed: {exc}", file=sys.stderr)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
