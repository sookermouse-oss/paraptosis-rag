from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_LITERATURE_DIR = Path("data/literature")
DEFAULT_XML_DIR = Path("fulltext_xml")

PAPER_FIELDS = [
    "paper_id",
    "doi",
    "pmid",
    "pmcid",
    "normalized_title",
    "title",
    "authors",
    "journal_title",
    "journal_iso",
    "issn",
    "publication_date",
    "year",
    "mesh_terms",
    "publication_types",
    "grants",
    "openalex_id",
    "cited_by_count",
    "publication_year",
    "open_access_oa_status",
    "open_access_is_oa",
    "type",
    "concepts",
    "identifier_conflicts",
    "updated_at",
]

SOURCE_RECORD_FIELDS = [
    "source_record_id",
    "paper_id",
    "source",
    "source_id",
    "doi",
    "pmid",
    "pmcid",
    "title",
    "publication_date",
    "source_url",
    "matched_keywords",
    "raw_json",
    "updated_at",
]

CONTENT_ASSET_FIELDS = [
    "content_asset_id",
    "paper_id",
    "source_record_id",
    "source",
    "content_type",
    "status",
    "remote_url",
    "local_path",
    "mime_type",
    "text",
    "updated_at",
]


class LiteratureStore:
    def __init__(self, data_dir: Path = DEFAULT_LITERATURE_DIR) -> None:
        self.data_dir = data_dir
        self.papers = load_json_table(data_dir / "papers.json")
        self.source_records = load_json_table(data_dir / "source_records.json")
        self.content_assets = load_json_table(data_dir / "content_assets.json")

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.deduplicate_papers()
        self.source_records = deduplicate_source_records(
            [normalize_existing_source_record(row) for row in self.source_records]
        )
        self.content_assets = deduplicate_content_assets(
            [normalize_existing_content_asset(row) for row in self.content_assets]
        )
        write_table(self.data_dir / "papers", PAPER_FIELDS, self.papers)
        write_table(self.data_dir / "source_records", SOURCE_RECORD_FIELDS, self.source_records)
        write_table(self.data_dir / "content_assets", CONTENT_ASSET_FIELDS, self.content_assets)

    def upsert_paper(self, candidate: dict[str, Any]) -> dict[str, str]:
        normalized = normalize_paper(candidate)
        existing = self.find_paper(normalized)
        if existing is None:
            key = first_dedupe_key(normalized)
            normalized["paper_id"] = stable_id("paper", key)
            normalized["updated_at"] = utc_now()
            self.papers.append(blank_row(PAPER_FIELDS) | normalized)
            return self.papers[-1]

        merge_nonempty(existing, normalized)
        self.merge_duplicate_papers(existing)
        existing["updated_at"] = utc_now()
        return existing

    def find_paper(self, paper: dict[str, Any]) -> dict[str, str] | None:
        return find_matching_paper(self.papers, paper)

    def deduplicate_papers(self) -> None:
        deduped: list[dict[str, str]] = []
        for paper in self.papers:
            normalized = blank_row(PAPER_FIELDS) | normalize_paper(paper)
            normalized["paper_id"] = paper.get("paper_id", "") or stable_id("paper", first_dedupe_key(normalized))
            normalized["updated_at"] = paper.get("updated_at", "") or utc_now()
            normalized["identifier_conflicts"] = paper.get("identifier_conflicts", "")

            existing = find_matching_paper(deduped, normalized)
            if existing is None:
                deduped.append(normalized)
                continue

            old_paper_id = normalized["paper_id"]
            merge_paper_rows(existing, normalized)
            self.reassign_paper_id(old_paper_id, existing["paper_id"])
        self.papers = deduped

    def merge_duplicate_papers(self, paper: dict[str, str]) -> None:
        for duplicate in list(self.papers):
            if duplicate is paper:
                continue
            if papers_match(paper, duplicate):
                old_paper_id = duplicate.get("paper_id", "")
                merge_paper_rows(paper, duplicate)
                self.reassign_paper_id(old_paper_id, paper["paper_id"])
                self.papers.remove(duplicate)

    def reassign_paper_id(self, old_paper_id: str, new_paper_id: str) -> None:
        if not old_paper_id or old_paper_id == new_paper_id:
            return
        for row in self.source_records:
            if row.get("paper_id") == old_paper_id:
                row["paper_id"] = new_paper_id
        for row in self.content_assets:
            if row.get("paper_id") == old_paper_id:
                row["paper_id"] = new_paper_id

    def upsert_source_record(self, record: dict[str, Any]) -> dict[str, str]:
        normalized = normalize_source_record(record)
        source = normalized.get("source", "")
        source_id = normalized.get("source_id", "")
        paper_id = normalized.get("paper_id", "")
        if source_id:
            source_record_id = stable_id("source", f"{source}:{source_id}")
        else:
            source_record_id = stable_id("source", f"{source}:{paper_id}:{normalized.get('title', '')}")
        normalized["source_record_id"] = source_record_id

        existing = find_by_id(self.source_records, "source_record_id", source_record_id)
        if existing is None:
            normalized["updated_at"] = utc_now()
            self.source_records.append(blank_row(SOURCE_RECORD_FIELDS) | normalized)
            return self.source_records[-1]

        merge_source_record(existing, normalized)
        existing["updated_at"] = utc_now()
        return existing

    def upsert_content_asset(self, asset: dict[str, Any]) -> dict[str, str]:
        normalized = normalize_content_asset(asset)
        normalized["content_asset_id"] = content_asset_id(normalized)

        existing = find_by_id(self.content_assets, "content_asset_id", normalized["content_asset_id"])
        if existing is None:
            normalized["updated_at"] = utc_now()
            self.content_assets.append(blank_row(CONTENT_ASSET_FIELDS) | normalized)
            return self.content_assets[-1]

        merge_content_asset(existing, normalized)
        existing["updated_at"] = utc_now()
        return existing


def load_json_table(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [{str(key): stringify(value) for key, value in row.items()} for row in data if isinstance(row, dict)]


def write_table(base_path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    json_path = base_path.with_suffix(".json")
    csv_path = base_path.with_suffix(".csv")
    normalized_rows = [{field: stringify(row.get(field, "")) for field in fields} for row in rows]
    json_path.write_text(json.dumps(normalized_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(normalized_rows)


def normalize_paper(candidate: dict[str, Any]) -> dict[str, str]:
    title = clean_text(candidate.get("title", ""))
    publication_date = clean_text(candidate.get("publication_date") or candidate.get("date", ""))
    return {
        "doi": normalize_doi(candidate.get("doi", "")),
        "pmid": clean_text(candidate.get("pmid", "")),
        "pmcid": normalize_pmcid(candidate.get("pmcid", "")),
        "normalized_title": normalize_title(title),
        "title": title,
        "authors": clean_text(candidate.get("authors", "")),
        "journal_title": clean_text(candidate.get("journal_title", "")),
        "journal_iso": clean_text(candidate.get("journal_iso", "")),
        "issn": clean_text(candidate.get("issn", "")),
        "publication_date": publication_date,
        "year": extract_year(publication_date),
        "mesh_terms": encode_list(candidate.get("mesh_terms", "")),
        "publication_types": encode_list(candidate.get("publication_types", "")),
        "grants": encode_json(candidate.get("grants", "")),
        "openalex_id": clean_text(candidate.get("openalex_id", "")),
        "cited_by_count": clean_text(candidate.get("cited_by_count", "")),
        "publication_year": clean_text(candidate.get("publication_year", "")),
        "open_access_oa_status": clean_text(candidate.get("open_access_oa_status", "")),
        "open_access_is_oa": clean_text(candidate.get("open_access_is_oa", "")),
        "type": clean_text(candidate.get("type", "")),
        "concepts": encode_json(candidate.get("concepts", "")),
        "identifier_conflicts": clean_text(candidate.get("identifier_conflicts", "")),
    }


def normalize_source_record(record: dict[str, Any]) -> dict[str, str]:
    raw = record.get("raw_json", "")
    return {
        "paper_id": clean_text(record.get("paper_id", "")),
        "source": clean_text(record.get("source", "")),
        "source_id": clean_text(record.get("source_id", "")),
        "doi": normalize_doi(record.get("doi", "")),
        "pmid": clean_text(record.get("pmid", "")),
        "pmcid": normalize_pmcid(record.get("pmcid", "")),
        "title": clean_text(record.get("title", "")),
        "publication_date": clean_text(record.get("publication_date") or record.get("date", "")),
        "source_url": clean_text(record.get("source_url", "")),
        "matched_keywords": encode_list(record.get("matched_keywords", "")),
        "raw_json": raw if isinstance(raw, str) else encode_json(raw),
    }


def normalize_existing_source_record(record: dict[str, Any]) -> dict[str, str]:
    normalized = blank_row(SOURCE_RECORD_FIELDS) | normalize_source_record(record)
    normalized["source_record_id"] = clean_text(record.get("source_record_id", ""))
    normalized["updated_at"] = clean_text(record.get("updated_at", ""))
    return normalized


def normalize_content_asset(asset: dict[str, Any]) -> dict[str, str]:
    return {
        "paper_id": clean_text(asset.get("paper_id", "")),
        "source_record_id": clean_text(asset.get("source_record_id", "")),
        "source": clean_text(asset.get("source", "")),
        "content_type": clean_text(asset.get("content_type", "")),
        "status": clean_text(asset.get("status", "")),
        "remote_url": clean_text(asset.get("remote_url", "")),
        "local_path": clean_text(asset.get("local_path", "")),
        "mime_type": clean_text(asset.get("mime_type", "")),
        "text": clean_text(asset.get("text", "")),
    }


def normalize_existing_content_asset(asset: dict[str, Any]) -> dict[str, str]:
    normalized = blank_row(CONTENT_ASSET_FIELDS) | normalize_content_asset(asset)
    normalized["content_asset_id"] = content_asset_id(normalized)
    normalized["updated_at"] = clean_text(asset.get("updated_at", ""))
    return normalized


def dedupe_keys(paper: dict[str, Any]) -> list[str]:
    return [f"{name}:{value}" for name, value in dedupe_key_pairs(paper)]


def dedupe_key_pairs(paper: dict[str, Any]) -> list[tuple[str, str]]:
    doi = normalize_doi(paper.get("doi", ""))
    pmid = clean_text(paper.get("pmid", ""))
    pmcid = normalize_pmcid(paper.get("pmcid", ""))
    normalized_title = clean_text(paper.get("normalized_title", "")) or normalize_title(paper.get("title", ""))
    keys: list[tuple[str, str]] = []
    if doi:
        keys.append(("doi", doi))
    if pmid:
        keys.append(("pmid", pmid))
    if pmcid:
        keys.append(("pmcid", pmcid))
    if normalized_title:
        keys.append(("title", normalized_title))
    return keys


def has_dedupe_key(paper: dict[str, Any], key_name: str, key_value: str) -> bool:
    return any(name == key_name and value == key_value for name, value in dedupe_key_pairs(paper))


def find_matching_paper(rows: list[dict[str, str]], paper: dict[str, Any]) -> dict[str, str] | None:
    for row in rows:
        if normalize_doi(paper.get("doi", "")) and normalize_doi(paper.get("doi", "")) == normalize_doi(row.get("doi", "")):
            return row
    for row in rows:
        if clean_text(paper.get("pmid", "")) and clean_text(paper.get("pmid", "")) == clean_text(row.get("pmid", "")):
            return row
    for row in rows:
        if normalize_pmcid(paper.get("pmcid", "")) and normalize_pmcid(paper.get("pmcid", "")) == normalize_pmcid(row.get("pmcid", "")):
            if not has_strong_identifier_conflict(row, paper):
                return row
    for row in rows:
        normalized_title = clean_text(paper.get("normalized_title", "")) or normalize_title(paper.get("title", ""))
        row_title = clean_text(row.get("normalized_title", "")) or normalize_title(row.get("title", ""))
        if normalized_title and normalized_title == row_title:
            return row
    return None


def papers_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return find_matching_paper([right], left) is not None


def has_strong_identifier_conflict(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    for key, normalizer in [
        ("doi", normalize_doi),
        ("pmid", clean_text),
    ]:
        left = normalizer(existing.get(key, ""))
        right = normalizer(incoming.get(key, ""))
        if left and right and left != right:
            return True
    return False


def first_dedupe_key(paper: dict[str, Any]) -> str:
    keys = dedupe_keys(paper)
    if keys:
        return keys[0]
    return f"title:{normalize_title(paper.get('title', 'untitled'))}"


def merge_nonempty(existing: dict[str, str], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        text = stringify(value)
        if key in {"doi", "pmid", "pmcid", "normalized_title"} and text and existing.get(key) and existing.get(key) != text:
            add_identifier_conflict(existing, key, existing.get(key, ""), text)
            continue
        if text and not existing.get(key):
            existing[key] = text
        elif text and key in {"mesh_terms", "publication_types"}:
            existing[key] = merge_semicolon_values(existing.get(key, ""), text)
        elif text and key in {"grants", "concepts"}:
            existing[key] = text
        elif text and key in {"openalex_id", "cited_by_count", "publication_year", "open_access_oa_status", "open_access_is_oa", "type"}:
            existing[key] = text


def merge_source_record(existing: dict[str, str], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        text = stringify(value)
        if not text or key == "source_record_id":
            continue
        if key in {"source", "source_id"} and existing.get(key) and existing.get(key) != text:
            continue
        existing[key] = text


def merge_content_asset(existing: dict[str, str], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        text = stringify(value)
        if not text or key == "content_asset_id":
            continue
        existing[key] = text


def merge_paper_rows(existing: dict[str, str], incoming: dict[str, Any]) -> None:
    merge_nonempty(existing, incoming)
    existing["updated_at"] = utc_now()


def add_identifier_conflict(row: dict[str, str], field: str, existing_value: str, incoming_value: str) -> None:
    conflict = f"{field}:{existing_value}!={incoming_value}"
    conflicts = [item.strip() for item in row.get("identifier_conflicts", "").split(";") if item.strip()]
    if conflict not in conflicts:
        conflicts.append(conflict)
    row["identifier_conflicts"] = "; ".join(conflicts)


def merge_semicolon_values(existing: str, incoming: str) -> str:
    values = []
    for raw in [existing, incoming]:
        for item in raw.split(";"):
            item = item.strip()
            if item and item not in values:
                values.append(item)
    return "; ".join(values)


def find_by_id(rows: list[dict[str, str]], key: str, value: str) -> dict[str, str] | None:
    for row in rows:
        if row.get(key) == value:
            return row
    return None


def deduplicate_source_records(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    for row in rows:
        existing = find_by_id(deduped, "source_record_id", row.get("source_record_id", ""))
        if existing is None:
            deduped.append(row)
            continue
        merge_source_record(existing, row)
        existing["updated_at"] = max(existing.get("updated_at", ""), row.get("updated_at", ""))
    return deduped


def deduplicate_content_assets(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    for row in rows:
        existing = find_by_id(deduped, "content_asset_id", row.get("content_asset_id", ""))
        if existing is None:
            deduped.append(row)
            continue
        merge_content_asset(existing, row)
        existing["updated_at"] = max(existing.get("updated_at", ""), row.get("updated_at", ""))
    return deduped


def content_asset_id(asset: dict[str, Any]) -> str:
    normalized = normalize_content_asset(asset)
    key = "|".join(
        [
            normalized.get("paper_id", ""),
            normalized.get("source_record_id", ""),
            normalized.get("source", ""),
            normalized.get("content_type", ""),
            normalized.get("remote_url", ""),
            normalized.get("local_path", ""),
        ]
    )
    if not normalized.get("remote_url") and not normalized.get("local_path"):
        key += "|" + normalized.get("text", "")[:200]
    return stable_id("asset", key)


def normalize_doi(value: Any) -> str:
    return clean_text(value).lower().removeprefix("doi:")


def normalize_pmcid(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = text.removeprefix("pmcid:").removeprefix("PMCID:").strip()
    return f"PMC{text[3:]}" if text.upper().startswith("PMC") else f"PMC{text}"


def normalize_title(value: Any) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split())


def stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return encode_json(value)
    return str(value)


def encode_list(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(clean_text(item) for item in value if clean_text(item))
    return clean_text(value)


def encode_json(value: Any) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_json(value: str) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def extract_year(value: str) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", value)
    return match.group(0) if match else ""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def blank_row(fields: list[str]) -> dict[str, str]:
    return {field: "" for field in fields}


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "untitled"
