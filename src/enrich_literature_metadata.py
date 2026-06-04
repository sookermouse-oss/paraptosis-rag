from __future__ import annotations

import argparse
import time
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from src.literature_store import DEFAULT_LITERATURE_DIR, LiteratureStore, clean_text, decode_json, encode_json


PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
BIORXIV_API_BASE_URL = "https://api.biorxiv.org/details"
PREPRINT_WEB_BASE_URL = {
    "biorxiv": "https://www.biorxiv.org/content",
    "medrxiv": "https://www.medrxiv.org/content",
}
PREPRINT_DOI_PREFIX = "10.1101/"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_REQUEST_DELAY_SECONDS = 0.0
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
            "publication dates, and concepts. Preprint mode enriches existing bioRxiv/medRxiv DOI "
            "records through the bioRxiv API and adds preprint source records plus abstract assets. "
            "Does not download full text or PDFs."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", choices=["pubmed", "openalex", "preprint", "all"], required=True)
    parser.add_argument("--limit", type=int, default=None, help="Maximum papers to attempt per selected source.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_LITERATURE_DIR)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--mailto", help="Email passed to OpenAlex polite pool. Required for --source openalex/all.")
    parser.add_argument(
        "--preprint-server",
        choices=["biorxiv", "medrxiv", "all"],
        default="all",
        help="Preprint server to query for --source preprint/all.",
    )
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS, help="Seconds to sleep between preprint API calls.")
    parser.add_argument(
        "--skip-if-published",
        action="store_true",
        help="Skip a preprint when its published-version DOI is already a paper in the store.",
    )
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
    if args.source in {"preprint", "all"}:
        enrich_preprints(
            store,
            args.limit,
            args.timeout,
            args.preprint_server,
            args.request_delay,
            args.skip_if_published,
        )
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

        updated_paper = store.upsert_paper(merge_fields(paper, metadata))
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
        updated_paper = store.upsert_paper(merge_fields(paper, metadata))
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


def enrich_preprints(
    store: LiteratureStore,
    limit: int | None,
    timeout: int,
    preprint_server: str,
    request_delay: float,
    skip_if_published: bool,
) -> None:
    candidates = preprint_candidates_from_store(store)
    known_dois = existing_paper_dois(store)
    attempted = 0
    enriched = 0
    published_overlap = 0
    skipped = 0
    for candidate in candidates:
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        doi = candidate["doi"]

        server, record = fetch_preprint_record_any_server(preprint_server, doi, timeout, request_delay)
        if server is None or record is None:
            print_progress("Preprint", attempted, enriched, "DOI", doi)
            continue

        published_doi = published_version_doi(record)
        if published_doi and published_doi.lower() in known_dois:
            published_overlap += 1
            if skip_if_published:
                skipped += 1
                print(f"Skipping {doi}: published version {published_doi} already in store.", flush=True)
                print_progress("Preprint", attempted, enriched, "DOI", doi)
                continue

        paper = upsert_preprint_paper_if_needed(store, candidate["paper"], paper_from_preprint(record, server))
        source_record = store.upsert_source_record(
            source_record_from_preprint(record, paper["paper_id"], candidate["matched_keywords"], server)
        )
        abstract = clean_text(record.get("abstract", ""))
        if abstract:
            store.upsert_content_asset(
                {
                    "paper_id": paper["paper_id"],
                    "source_record_id": source_record["source_record_id"],
                    "source": server,
                    "content_type": "abstract",
                    "status": "available",
                    "remote_url": source_record.get("source_url", ""),
                    "mime_type": "text/plain",
                    "text": abstract,
                }
            )
        enriched += 1
        print_progress("Preprint", attempted, enriched, "DOI", doi)

    print(f"Preprint DOI candidates: {len(candidates)}", flush=True)
    print(f"Preprint DOIs attempted: {attempted}", flush=True)
    print(f"Preprint DOIs enriched: {enriched}", flush=True)
    print(
        f"Preprints whose published version is already in the store: {published_overlap}"
        + (f" (skipped {skipped})" if skip_if_published else " (kept)"),
        flush=True,
    )


def print_progress(source: str, attempted: int, enriched: int, current_label: str, current_id: str) -> None:
    if attempted % 10 == 0:
        print(f"{source} attempted {attempted}, enriched {enriched}, current {current_label} {current_id}", flush=True)


def merge_fields(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in new.items():
        if value not in ("", None, []):
            merged[key] = value
    return merged


def fill_missing_fields(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in new.items():
        if value not in ("", None, []) and not merged.get(key):
            merged[key] = value
    return merged


def upsert_preprint_paper_if_needed(
    store: LiteratureStore,
    existing_paper: dict[str, Any],
    preprint_metadata: dict[str, Any],
) -> dict[str, str]:
    merged = fill_missing_fields(existing_paper, preprint_metadata)
    if existing_paper and merged == existing_paper:
        return existing_paper
    return store.upsert_paper(merged)


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


def fetch_preprint_record_any_server(
    preprint_server: str,
    doi: str,
    timeout: int,
    request_delay: float,
) -> tuple[str | None, dict[str, Any] | None]:
    servers = list(PREPRINT_WEB_BASE_URL) if preprint_server == "all" else [preprint_server]
    for server in servers:
        record = fetch_preprint_record(server, doi, timeout)
        if request_delay:
            time.sleep(request_delay)
        if record is not None:
            return server, record
    return None, None


def fetch_preprint_record(server: str, doi: str, timeout: int) -> dict[str, Any] | None:
    url = f"{BIORXIV_API_BASE_URL}/{server}/{doi}/na/json"
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "paraptosis-rag-preprint-enrichment/1.0"},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"{server} lookup attempt {attempt}/{REQUEST_RETRIES} failed for {doi}: {exc}", file=sys.stderr)
            continue

        collection = payload.get("collection", [])
        if not collection:
            return None
        return max(collection, key=lambda row: safe_int(row.get("version")))
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


def preprint_candidates_from_store(store: LiteratureStore) -> list[dict[str, Any]]:
    paper_by_id = {paper.get("paper_id", ""): paper for paper in store.papers}
    candidates: dict[str, dict[str, Any]] = {}

    for paper in store.papers:
        doi = clean_text(paper.get("doi", "")).lower()
        if doi.startswith(PREPRINT_DOI_PREFIX):
            candidates[doi] = {"doi": doi, "paper": paper, "matched_keywords": []}

    for source_record in store.source_records:
        raw = decode_json(source_record.get("raw_json", ""))
        source_record_doi = clean_text(source_record.get("doi", "")).lower()
        raw_doi = clean_text(raw.get("doi", "")).lower() if isinstance(raw, dict) else ""
        doi = source_record_doi or raw_doi
        if not doi.startswith(PREPRINT_DOI_PREFIX):
            continue
        paper = paper_by_id.get(source_record.get("paper_id", ""), {})
        if doi not in candidates:
            candidates[doi] = {"doi": doi, "paper": paper, "matched_keywords": []}
        matched_keywords = decode_json_list(source_record.get("matched_keywords", ""))
        for keyword in matched_keywords:
            if keyword not in candidates[doi]["matched_keywords"]:
                candidates[doi]["matched_keywords"].append(keyword)

    return list(candidates.values())


def existing_paper_dois(store: LiteratureStore) -> set[str]:
    dois: set[str] = set()
    for paper in store.papers:
        for key in ("doi", "published_doi"):
            value = paper.get(key)
            if value:
                dois.add(str(value).lower())
    return dois


def paper_from_preprint(record: dict[str, Any], server: str) -> dict[str, Any]:
    return {
        "doi": clean_text(record.get("doi", "")),
        "title": clean_text(record.get("title", "")),
        "authors": clean_text(record.get("authors", "")),
        "publication_date": clean_text(record.get("date", "")),
    }


def source_record_from_preprint(
    record: dict[str, Any],
    paper_id: str,
    matched_keywords: list[str],
    server: str,
) -> dict[str, Any]:
    doi = clean_text(record.get("doi", ""))
    version = str(safe_int(record.get("version")) or 1)
    source_url = f"{PREPRINT_WEB_BASE_URL[server]}/{doi}v{version}" if doi else ""
    published_doi = published_version_doi(record)

    raw_record = dict(record)
    raw_record["preprint_server"] = server
    raw_record["preprint_version"] = version
    raw_record["biorxiv_jatsxml_url"] = clean_text(record.get("jatsxml", ""))
    raw_record["preprint_license"] = clean_text(record.get("license", ""))
    raw_record["preprint_category"] = clean_text(record.get("category", ""))
    raw_record["link_pdf"] = f"{source_url}.full.pdf" if source_url else ""
    if published_doi:
        raw_record["published_doi"] = published_doi

    return {
        "paper_id": paper_id,
        "source": server,
        "source_id": doi,
        "doi": doi,
        "pmid": "",
        "pmcid": "",
        "title": clean_text(record.get("title", "")),
        "publication_date": clean_text(record.get("date", "")),
        "source_url": source_url,
        "matched_keywords": matched_keywords,
        "raw_json": encode_json(raw_record),
    }


def published_version_doi(record: dict[str, Any]) -> str:
    published = clean_text(str(record.get("published", "")))
    return "" if published.upper() in ("", "NA") else published


def decode_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    decoded = decode_json(value)
    if isinstance(decoded, list):
        return [clean_text(item) for item in decoded if clean_text(item)]
    if value:
        return [item.strip() for item in str(value).split(";") if item.strip()]
    return []


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


def safe_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
