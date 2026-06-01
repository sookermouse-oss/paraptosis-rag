# paraptosis-rag

Minimal literature acquisition, ingestion, retrieval, and RAG answering pipeline for PMC full-text XML files.

## Stage 0: Literature Metadata and XML Acquisition

This stage keeps literature identity separate from content availability. One canonical paper can have many source records and many content assets.

It writes three canonical tables under `data/literature/`:

- `papers.csv/json`
- `source_records.csv/json`
- `content_assets.csv/json`

Papers are deduplicated by DOI first, then PMID, then PMCID, then normalized title. Content availability lives only in `content_assets`; there is no global availability status on `papers`.

Discovery and enrichment entry points:

```bash
./scripts/fetch-literature.sh
./scripts/enrich-literature.sh --source pubmed --limit 100
./scripts/enrich-literature.sh --source openalex --limit 100 --mailto you@example.com
./scripts/enrich-literature.sh --source all --limit 100 --mailto you@example.com
./scripts/fetch-fulltext-xml.sh --max-downloads 100
```

`fetch-literature.sh` runs `src.literature_discovery`, which uses Europe PMC as the main discovery source and searches `TITLE_ABS` when keywords are provided.

Default discovery settings are defined in `src/literature_discovery.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `keywords` | empty | Search terms matched against title/abstract. Empty means no keyword filter. |
| `days_back` | `1460` | Date window for Europe PMC discovery. |
| `timeout` | `30` | Per-request timeout. |
| `source_page_size` | `100` | Page size per source request. |
| `max_records` | `1000` | Safety cap for Europe PMC records scanned. |

Run a metadata scan:

```bash
./scripts/fetch-literature.sh
```

Override keywords or limits at runtime:

```bash
./scripts/fetch-literature.sh --keywords "paraptosis,methuosis" --max-records 50
```

Download full-text XML for records in the master metadata:

```bash
./scripts/fetch-fulltext-xml.sh --max-downloads 100
```

Acquisition metadata is written under `data/literature/`. Downloaded XML is written to `fulltext_xml/`. Both directories are local data outputs and are not committed. The pipeline does not create GPT Markdown exports and never downloads PDFs automatically.

Useful acquisition CLI defaults:

| Command | Parameter | Default |
| --- | --- | --- |
| `fetch-literature.sh` | `--keywords` / `--keyword` | unset; no keyword filter |
| `fetch-literature.sh` | `--days-back` | `1460` |
| `fetch-literature.sh` | `--timeout` | `30` |
| `fetch-literature.sh` | `--source-page-size` | `100` |
| `fetch-literature.sh` | `--max-records` | `1000` |
| `fetch-literature.sh` | `--data-dir` | `data/literature` |
| `enrich-literature.sh` | `--source` | required: `pubmed`, `openalex`, or `all` |
| `enrich-literature.sh` | `--limit` | unset |
| `enrich-literature.sh` | `--data-dir` | `data/literature` |
| `enrich-literature.sh` | `--timeout` | `30` |
| `enrich-literature.sh` | `--mailto` | required for `openalex` or `all` |
| `fetch-fulltext-xml.sh` | `--data-dir` | `data/literature` |
| `fetch-fulltext-xml.sh` | `--timeout` | `30` |
| `fetch-fulltext-xml.sh` | `--limit` | unset |
| `fetch-fulltext-xml.sh` | `--max-downloads` | unset |
| `fetch-fulltext-xml.sh` | `--xml-dir` | `fulltext_xml` |

Large metadata enrichment runs call external APIs one paper at a time and can take several minutes. `enrich-literature.sh` prints each request and reports progress every 10 attempted papers.

## Stage 1: XML to Nodes JSONL

This stage reads PMC/JATS XML files from:

```bash
fulltext_xml/
```

It parses article metadata, abstract text, and body sections, chunks each section, and writes:

```bash
data/nodes.jsonl
data/metadata.jsonl
```

No OpenAI Vector Store or Qdrant integration is used.

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

To use XML files from another local directory:

```bash
python3 src/build_nodes.py --xml-dir /path/to/fulltext_xml --limit 500
```

Run local BM25 search tests:

```bash
python3 src/search_nodes.py
python3 src/search_nodes.py --query "calcium homeostasis" --top-k 5
python3 src/search_nodes.py --query "paraptosis in breast cancer prognosis" --top-k 10 --exclude-section-type methods
```

Build a local LlamaIndex `SimpleVectorStore` from `data/nodes.jsonl` with the HuggingFace biomedical embedding model `pritamdeka/S-PubMedBert-MS-MARCO`:

```bash
python3 src/llamaindex_retrieval.py build
python3 src/llamaindex_retrieval.py search --query "How does ER stress induce paraptosis?" --top-k 10
python3 src/llamaindex_retrieval.py search --query "paraptosis in breast cancer prognosis" --top-k 10 --exclude-section-type methods
python3 src/llamaindex_retrieval.py benchmark --top-k 10 --exclude-section-type methods
```

The local LlamaIndex index is stored in `data/llamaindex_storage/`. This is a retriever only; it does not use OpenAI APIs, OpenAI Vector Store, Qdrant, chat agents, or RAG answering.

Run hybrid retrieval with BM25 plus LlamaIndex embeddings. The default hybrid score uses BM25 weight `0.2`, embedding weight `0.8`, and single-source penalties for BM25-only and embedding-only hits:

```bash
python src/hybrid_retrieval.py \
  --query "How does ER stress induce paraptosis?" \
  --top-k 10 \
  --exclude-section-type methods
```

Run the default five-query hybrid benchmark. Diversity is disabled by default; pass `--max-chunks-per-pmcid 2` to keep at most two chunks from the same PMCID:

```bash
python src/hybrid_retrieval.py \
  --top-k 10 \
  --exclude-section-type methods \
  --max-chunks-per-pmcid 2
```

Run minimal RAG answering with hybrid retrieval plus the OpenAI Responses API. Set `OPENAI_API_KEY` in the environment; the key is not stored in the repo. Defaults use hybrid retrieval with BM25 weight `0.2`, embedding weight `0.8`, single-source penalties enabled, `--exclude-section-type methods`, and `--top-k 8`:

```bash
export OPENAI_API_KEY="..."

python src/rag_answer.py \
  --query "Can paraptosis help overcome drug resistance?"

python src/rag_answer.py \
  --query "What is the role of PI4KB in paraptosis?"
```

Before printing the answer, `rag_answer.py` reports retrieval strength for the returned evidence:

- `HIGH`: `top1_score >= 0.85` and `top5_avg_score >= 0.45`
- `MEDIUM`: `top1_score >= 0.6` and `top5_avg_score >= 0.25`
- `LOW`: anything below those thresholds

The diagnostics block also prints evidence diversity, unique PMCID counts, `query_coverage`, plus `content_type` and `section_type` distributions. A `LOW` retrieval strength prints a warning, but it does not switch to GPT-only mode, block answering, or change the retrieval ranking/prompt.

`query_coverage` is a separate diagnostic that checks whether important non-anchor query terms appear in the retrieved evidence. It is displayed for review, but it does not downgrade `HIGH` / `MEDIUM` / `LOW` retrieval strength.

## Common commands

Run these from the project root:

```bash
./scripts/rag-build-500.sh
./scripts/rag-benchmark.sh
./scripts/fetch-literature.sh
./scripts/enrich-literature.sh --source pubmed --limit 100
./scripts/enrich-literature.sh --source openalex --limit 100 --mailto you@example.com
./scripts/fetch-fulltext-xml.sh --max-downloads 100
./scripts/rag-context.sh "What is the role of PI4KB in paraptosis?"
./scripts/rag-answer.sh "Can paraptosis help overcome drug resistance?"
./scripts/rag-compare.sh
```

`rag-answer.sh`, `rag-context.sh`, and `rag-compare.sh` use the OpenAI API through `OPENAI_API_KEY`. `rag-context.sh` also writes the latest debug context to `data/debug_context.txt`.

Run the Chinese RAG evaluation workflow:

```bash
./scripts/rag-eval-zh.sh
```

Each JSONL row contains at least:

- `node_id`
- `text`
- `pmcid`
- `title`
- `year`
- `journal`
- `section_title`
- `section_type`
- `section_index`
- `source_file`

Article-level metadata, including `authors` and `abstract`, is written once per article to `data/metadata.jsonl`.

The raw XML files are read in place and are not modified.
