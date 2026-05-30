# paraptosis-rag

Minimal ingestion pipeline for PMC full-text XML files.

## Stage 1: XML to Nodes JSONL

This stage reads PMC/JATS XML files from:

```bash
/Users/shuangsu/Documents/Projects/paraptosis-biorxiv-job/fulltext_xml
```

It parses article metadata, abstract text, and body sections, chunks each section, and writes:

```bash
data/nodes.jsonl
data/metadata.jsonl
```

No embeddings, OpenAI API calls, OpenAI Vector Store, or Qdrant integration are used.

## Setup

```bash
pip install -r requirements.txt
```

## Run

Build nodes for all XML files:

```bash
python3 src/build_nodes.py
```

Smoke test on the first 3 XML files:

```bash
python3 src/build_nodes.py --limit 3
```

Each JSONL row contains at least:

- `node_id`
- `text`
- `pmcid`
- `title`
- `year`
- `journal`
- `section_title`
- `section_index`
- `source_file`

Article-level metadata, including `authors` and `abstract`, is written once per article to `data/metadata.jsonl`.

The raw XML files are read in place and are not modified.
