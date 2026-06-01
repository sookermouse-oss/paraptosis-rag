from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

from src.literature_store import (
    DEFAULT_LITERATURE_DIR,
    DEFAULT_XML_DIR,
    LiteratureStore,
    decode_json,
    normalize_pmcid,
    safe_filename,
)


PMC_OAI_URL = "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/"
DEFAULT_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download XML content assets for known papers. For papers with PMCID, downloads PMC JATS XML; "
            "for Europe PMC records with fullTextXML URLs, downloads Europe PMC full text XML. Adds "
            "content_assets rows with content_type=pmc_jats_xml or europepmc_fulltext_xml. Does not "
            "touch PDFs and does not store availability on papers."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_LITERATURE_DIR, help="Directory containing papers/source_records/content_assets tables.")
    parser.add_argument("--xml-dir", type=Path, default=DEFAULT_XML_DIR, help="Directory where XML files are saved.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Request timeout in seconds.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum papers to attempt.")
    parser.add_argument("--max-downloads", type=int, default=None, help="Stop after this many XML downloads.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = LiteratureStore(args.data_dir)
    args.xml_dir.mkdir(parents=True, exist_ok=True)
    attempted = 0
    downloaded = 0

    for paper in store.papers:
        if args.limit is not None and attempted >= args.limit:
            break
        if args.max_downloads is not None and downloaded >= args.max_downloads:
            break

        candidates = xml_candidates_for_paper(store, paper)
        if not candidates:
            continue
        attempted += 1

        for candidate in candidates:
            if args.max_downloads is not None and downloaded >= args.max_downloads:
                break
            output_path = args.xml_dir / xml_filename(paper, candidate["content_type"])
            if output_path.exists() and output_path.stat().st_size > 0:
                add_xml_asset(store, paper, candidate, output_path)
                continue
            if save_xml_from_url(candidate["remote_url"], output_path, args.timeout, candidate["content_type"]):
                add_xml_asset(store, paper, candidate, output_path)
                downloaded += 1

    store.save()
    print(f"Papers attempted for XML: {attempted}")
    print(f"XML files downloaded: {downloaded}")
    print(f"Wrote literature tables to {args.data_dir}")
    return 0


def xml_candidates_for_paper(store: LiteratureStore, paper: dict[str, str]) -> list[dict[str, str]]:
    candidates = []
    pmcid = normalize_pmcid(paper.get("pmcid", ""))
    source_record_id = ""
    if pmcid:
        source_record_id = first_source_record_id(store, paper["paper_id"], ["europe_pmc", "pubmed"])
        candidates.append(
            {
                "source": "pmc",
                "source_record_id": source_record_id,
                "content_type": "pmc_jats_xml",
                "remote_url": pmc_oai_url(pmcid),
            }
        )

    for source_record in store.source_records:
        if source_record.get("paper_id") != paper.get("paper_id"):
            continue
        raw = decode_json(source_record.get("raw_json", ""))
        url = raw.get("europe_pmc_fulltext_xml_url", "") if isinstance(raw, dict) else ""
        if url:
            candidates.append(
                {
                    "source": "europe_pmc",
                    "source_record_id": source_record.get("source_record_id", ""),
                    "content_type": "europepmc_fulltext_xml",
                    "remote_url": str(url),
                }
            )
    return dedupe_candidates(candidates)


def first_source_record_id(store: LiteratureStore, paper_id: str, sources: list[str]) -> str:
    for source in sources:
        for record in store.source_records:
            if record.get("paper_id") == paper_id and record.get("source") == source:
                return record.get("source_record_id", "")
    return ""


def dedupe_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    deduped = []
    for candidate in candidates:
        key = (candidate["content_type"], candidate["remote_url"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def add_xml_asset(
    store: LiteratureStore,
    paper: dict[str, str],
    candidate: dict[str, str],
    output_path: Path,
) -> None:
    store.upsert_content_asset(
        {
            "paper_id": paper["paper_id"],
            "source_record_id": candidate.get("source_record_id", ""),
            "source": candidate["source"],
            "content_type": candidate["content_type"],
            "status": "available",
            "remote_url": candidate["remote_url"],
            "local_path": str(output_path),
            "mime_type": "application/xml",
        }
    )


def save_xml_from_url(url: str, output_path: Path, timeout: int, label: str) -> bool:
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={
                    "Accept": "application/xml,text/xml,*/*;q=0.5",
                    "User-Agent": "paraptosis-rag-xml-enrichment/1.0",
                },
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"{label} attempt {attempt}/{REQUEST_RETRIES} failed: {exc}", file=sys.stderr)
            continue

        content = response.content
        content_type = response.headers.get("content-type", "").lower()
        if "xml" not in content_type and not content.lstrip().startswith(b"<"):
            print(f"{label} returned non-XML content: {content_type}", file=sys.stderr)
            return False
        try:
            ET.fromstring(content)
        except ET.ParseError as exc:
            print(f"{label} returned invalid XML: {exc}", file=sys.stderr)
            return False

        output_path.write_bytes(content)
        print(f"Saved {label}: {output_path}")
        return True
    return False


def xml_filename(paper: dict[str, str], content_type: str) -> str:
    identifier = paper.get("doi") or paper.get("pmcid") or paper.get("pmid") or paper.get("paper_id")
    suffix = "pmc.xml" if content_type == "pmc_jats_xml" else "europepmc.xml"
    return f"{safe_filename(identifier)}.{suffix}"


def pmc_oai_url(pmcid: str) -> str:
    numeric_id = normalize_pmcid(pmcid).removeprefix("PMC")
    return (
        f"{PMC_OAI_URL}?verb=GetRecord"
        f"&identifier=oai:pubmedcentral.nih.gov:{numeric_id}"
        f"&metadataPrefix=pmc"
    )


if __name__ == "__main__":
    raise SystemExit(main())
