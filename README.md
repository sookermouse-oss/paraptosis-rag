# paraptosis-rag

Literature acquisition, ingestion, hybrid retrieval, RAG answering, and evaluation pipeline for molecular cell biology papers. The corpus can combine literature metadata, abstract assets, and locally downloaded PMC/JATS full-text XML.

## Stage 0: Literature Metadata and XML Acquisition

This stage keeps literature identity separate from content availability. One canonical paper can have many source records and many content assets.

The goal is to build a reproducible local literature catalog before any RAG indexing happens. Discovery decides which papers exist; enrichment adds metadata; XML acquisition downloads available full text. These are intentionally separate steps so a paper can be known even when full text is not available locally.

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

What each script does:

| Script | Main job | Does not do |
| --- | --- | --- |
| `fetch-literature.sh` | Discover candidate papers from Europe PMC and write canonical literature tables. | Does not download full text. |
| `enrich-literature.sh --source pubmed` | Add PubMed EFetch metadata such as MeSH terms, publication types, journal fields, and grants. | Does not fetch full text. |
| `enrich-literature.sh --source openalex` | Add OpenAlex metadata such as citation count, OA status, publication dates, and concepts. | Does not create content assets. |
| `fetch-fulltext-xml.sh` | Download PMC/JATS XML for records with available XML links. | Does not download PDFs. |

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

`data/nodes.jsonl` contains both:

- abstract nodes: `content_type=abstract`, `section_type=abstract`, `section_title=Abstract`
- fulltext nodes: `content_type=fulltext`

Each node is a retrievable evidence unit. It keeps both text and metadata so downstream retrieval can filter methods sections, distinguish abstracts from full text, and show evidence provenance in RAG answers.

Important ingestion behavior:

- Abstracts come from `data/literature/content_assets.json` when available.
- Fulltext chunks come from parsed PMC/JATS XML under `fulltext_xml/`.
- Abstracts are not mixed into body sections.
- Back matter and citation residue cleanup happens before chunking.
- Chunk size, overlap, embedding model, and retrieval ranking are separate from this stage.

No OpenAI Vector Store or Qdrant integration is used.

## Checked-in Data Snapshot

The repository can include a small reusable data snapshot:

| File | Purpose |
| --- | --- |
| `data/nodes.jsonl` | Retrieval-ready abstract and fulltext nodes. |
| `data/metadata.jsonl` | Article-level metadata generated from XML parsing. |
| `data/literature/papers.json` | Canonical paper records from literature discovery/enrichment. |
| `data/literature/content_assets.json` | Abstract/fulltext content availability records. |

If these files already exist after cloning or pulling the repo:

- You do not need to rerun literature discovery just to inspect or retrieve over the current snapshot.
- You do not need to rerun `src/build_nodes.py` unless you changed `fulltext_xml/`, `data/literature/content_assets.json`, parsing logic, or chunking logic.
- You can run BM25/hybrid retrieval directly from `data/nodes.jsonl`.
- You still need `data/llamaindex_storage/` for embedding retrieval. If it is missing, rebuild it from the checked-in `data/nodes.jsonl`:

```bash
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1 \
  .venv/bin/python src/llamaindex_retrieval.py build
```

If the HuggingFace embedding model is not cached locally, run the same build once without `HF_HUB_OFFLINE=1` to populate the cache.

## Setup

```bash
pip install -r requirements.txt
```

Recommended local environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Set `OPENAI_API_KEY` only for scripts that call OpenAI, such as `rag_answer.py`, Chinese query normalization, and GPT-only vs RAG evaluation. Retrieval/indexing commands do not require OpenAI.

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

The embedding step converts each `chunk text` into a vector. At query time, the query is also converted into a vector, and semantically similar chunks are retrieved by vector similarity. The selected model is biomedical rather than general-purpose because gene names, protein names, pathways, drugs, and cell-death terminology matter for this corpus.

Offline mode:

- Shell wrappers default to `HF_HUB_OFFLINE=1` where appropriate.
- If the embedding model is already cached locally, indexing/search will not contact HuggingFace Hub.
- If the model is not cached, run once without offline mode to populate the local cache.

Run hybrid retrieval with BM25 plus LlamaIndex embeddings. The default hybrid score uses BM25 weight `0.2`, embedding weight `0.8`, and single-source penalties for BM25-only and embedding-only hits:

```bash
python src/hybrid_retrieval.py \
  --query "How does ER stress induce paraptosis?" \
  --top-k 10 \
  --exclude-section-type methods
```

Retrieval scripts support `content_type` filters. By default, both abstract and fulltext nodes are searched:

```bash
python src/hybrid_retrieval.py \
  --query "paraptosis drug resistance" \
  --top-k 10 \
  --include-content-type abstract

python src/hybrid_retrieval.py \
  --query "paraptosis drug resistance" \
  --top-k 10 \
  --exclude-content-type abstract
```

Run the default five-query hybrid benchmark. Diversity is disabled by default; pass `--max-chunks-per-pmcid 2` to keep at most two chunks from the same PMCID:

```bash
python src/hybrid_retrieval.py \
  --top-k 10 \
  --exclude-section-type methods \
  --max-chunks-per-pmcid 2
```

Hybrid retrieval details:

| Step | Description |
| --- | --- |
| BM25 Top 30 | Lexical search over `data/nodes.jsonl`. Useful for exact terms such as gene and drug names. |
| Embedding Top 30 | Semantic search over `data/llamaindex_storage/`. Useful when wording differs but meaning is similar. |
| Merge and normalize | Scores from both sources are normalized before weighted combination. |
| Single-source penalty | BM25-only hits are multiplied by `0.4`; embedding-only hits by `0.8`; hits from both are not penalized. |
| Final Top K | Results are sorted by hybrid score and returned to the caller. |

Run minimal RAG answering with hybrid retrieval plus the OpenAI Responses API. Set `OPENAI_API_KEY` in the environment; the key is not stored in the repo. Defaults use hybrid retrieval with BM25 weight `0.2`, embedding weight `0.8`, single-source penalties enabled, `--exclude-section-type methods`, and `--top-k 8`:

```bash
export OPENAI_API_KEY="..."

python src/rag_answer.py \
  --query "Can paraptosis help overcome drug resistance?"

python src/rag_answer.py \
  --query "What is the role of PI4KB in paraptosis?"
```

Chinese questions are supported. For Chinese input, `rag_answer.py` first rewrites the question into an English biomedical retrieval query, retrieves English corpus evidence, then answers in Chinese:

```bash
python src/rag_answer.py \
  --query "PI4KB 在副凋亡中的作用是什么？"
```

High-risk project terms are protected during query rewrite by `config/terminology_zh_en.json`. This is intentionally a small project map, not a full biomedical dictionary. Current protected terms include:

| Chinese | English |
| --- | --- |
| `副凋亡` | `paraptosis` |
| `凋亡` | `apoptosis` |
| `铁死亡` | `ferroptosis` |
| `焦亡` | `pyroptosis` |
| `程序性坏死` | `necroptosis` |
| `自噬` | `autophagy` |

If the original Chinese query contains one of these terms, the normalized retrieval query must contain the mapped English term.

Before printing the answer, `rag_answer.py` reports retrieval strength for the returned evidence:

- `HIGH`: `top1_score >= 0.85` and `top5_avg_score >= 0.45`
- `MEDIUM`: `top1_score >= 0.6` and `top5_avg_score >= 0.25`
- `LOW`: anything below those thresholds

The diagnostics block also prints evidence diversity, unique PMCID counts, `query_coverage`, plus `content_type` and `section_type` distributions. A `LOW` retrieval strength prints a warning, but it does not switch to GPT-only mode, block answering, or change the retrieval ranking/prompt.

`query_coverage` is a separate diagnostic that checks whether important non-anchor query terms appear in the retrieved evidence. It is displayed for review, but it does not downgrade `HIGH` / `MEDIUM` / `LOW` retrieval strength.

RAG output includes:

- `Original query`
- `Normalized retrieval query`
- retrieval diagnostics
- `Answer`
- `Evidence` rows with `node_id`, title, `content_type`, year, cited-by count, section title, score, and preview

Answering rules:

- The model is instructed to answer only from retrieved nodes.
- If evidence is insufficient, it should say so instead of filling gaps from memory.
- Every key claim should include one or more `node_id` citations.
- Chinese input receives a Chinese answer; node IDs and gene/protein/drug names stay unchanged.

Use debug mode to inspect the exact retrieved context before OpenAI answer generation:

```bash
python src/rag_answer.py \
  --query "What is the role of PI4KB in paraptosis?" \
  --show-context \
  --save-context
```

`--save-context` writes `data/debug_context.txt`.

## Evaluation

The Chinese benchmark question set lives in:

```bash
data/eval_zh_questions.txt
```

Run the Chinese RAG-only evaluation:

```bash
./scripts/rag-eval-zh.sh
```

This writes:

```bash
data/eval_zh_answers.md
```

You can also run it on another question file or write to another report path:

```bash
./scripts/rag-eval-zh.sh \
  --questions-file data/eval_zh_questions.txt \
  --output-file data/eval_zh_answers.md
```

Run the GPT-only vs RAG Chinese benchmark:

```bash
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1 \
  .venv/bin/python src/benchmark_zh_rag_vs_gpt.py
```

This writes:

```bash
data/eval_zh_gpt_vs_rag.md
```

`data/eval_zh_gpt_vs_rag.md` is a reusable baseline report and can be committed with the benchmark question set. Timestamped ad hoc reports such as `data/eval_zh_gpt_vs_rag_pi4kb_check.md` remain ignored.

For a single-question GPT-only vs RAG test, create a temporary question file:

```bash
printf '# Check\n\n1. PI4KB 在副凋亡中的作用是什么？\n' > /tmp/pi4kb_question.txt

HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1 \
  .venv/bin/python src/benchmark_zh_rag_vs_gpt.py \
  --questions /tmp/pi4kb_question.txt \
  --output data/eval_zh_gpt_vs_rag_pi4kb_check.md
```

The benchmark report does not automatically score correctness. It records question, retrieval strength, evidence diversity, top scores, coverage, GPT-only answer, and RAG answer for manual review.

Run claim-level faithfulness checks on a GPT-only vs RAG report:

```bash
./scripts/rag-faithfulness.sh
```

The default command checks `data/eval_en_gpt_vs_rag.md` and writes:

```bash
data/eval_en_faithfulness.json
data/eval_en_faithfulness.md
```

By default this is deterministic and does not call OpenAI. It splits the RAG answer into claims, checks whether substantive claims have valid retrieved node citations, strips out-of-corpus citations, and reports bare claims. LLM judge mode is off by default.

To enable LLM judge against cited/retrieved chunk text:

```bash
./scripts/rag-faithfulness.sh \
  data/eval_en_gpt_vs_rag.md \
  --judge \
  --start-question 1 \
  --end-question 5
```

Use `--judge` only when you want OpenAI calls; `OPENAI_API_KEY` is required in that mode.

The current Chinese benchmark is intentionally mixed:

| Group | Purpose |
| --- | --- |
| HIGH | Questions where corpus-grounded RAG should clearly help, such as ER stress, PI4KB, drug resistance, and prognosis. |
| MEDIUM | Cell-death comparison questions where general knowledge may help but retrieval coverage can be uneven. |
| LOW corpus-out | Questions that may be biologically plausible but are outside the current corpus. |
| LOW irrelevant | Unrelated questions such as weather or stock market, used to test refusal/low-evidence behavior. |
| Query rewrite stress | Chinese phrasing designed to test whether normalized English retrieval queries preserve the intended biomedical meaning. |

Manual review should look at:

- whether the RAG answer is more specific than GPT-only
- whether key claims are supported by evidence
- whether citations point to relevant nodes
- whether low-evidence questions are handled conservatively
- whether failures come from retrieval, query rewrite, diagnostics, or answer generation

## Local Web UI

Start the local GPT-only vs RAG evaluation web UI:

```bash
./scripts/rag-eval-web.sh
```

Then open:

```text
http://127.0.0.1:8765
```

Do not open `docs/rag_eval_web.html` directly with `file://`; the page needs the local Python server because the browser cannot execute shell scripts by itself.

The web UI accepts one Chinese question, calls `src/benchmark_zh_rag_vs_gpt.py`, and displays the generated markdown report. It is local-only; do not expose it to the public internet because the backend runs repository commands and calls the OpenAI API.

The web UI is a convenience wrapper around the same command-line benchmark. It writes timestamped markdown reports under `data/`, which are ignored by git. Web-generated reports older than 7 days are cleaned up by default when the web server runs. Use `--report-ttl-days 0` to disable cleanup:

```bash
./scripts/rag-eval-web.sh --report-ttl-days 0
```

If a browser shows `Failed to fetch`, it usually means the page was opened directly as a file or the local server is not running.

## Slides

A visual workflow deck is available at:

```bash
docs/rag_workflow_slides.html
```

Open it in a browser to present the Molecular Cell Biology RAG workflow. It covers ingestion, chunking, embeddings, hybrid retrieval, answer generation, and evaluation.

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
./scripts/rag-eval-zh.sh
./scripts/rag-faithfulness.sh
./scripts/rag-eval-web.sh
```

`rag-answer.sh`, `rag-context.sh`, `rag-compare.sh`, `rag-eval-zh.sh`, `rag-eval-web.sh`, and `src/benchmark_zh_rag_vs_gpt.py` use the OpenAI API through `OPENAI_API_KEY`. `rag-faithfulness.sh` uses OpenAI only when `--judge` is passed. `rag-context.sh` also writes the latest debug context to `data/debug_context.txt`.

Most RAG scripts set `HF_HUB_OFFLINE=1` by default through their shell wrappers, so the HuggingFace embedding model is loaded from local cache first. If the model is missing locally, run the build/search command without offline mode once to populate the cache.

## Generated Outputs

Most large or frequently regenerated local outputs are intentionally not committed:

| Path | Meaning |
| --- | --- |
| `data/llamaindex_storage/` | Local LlamaIndex vector store. |
| `data/debug_context.txt` | Latest saved RAG debug context. |
| `data/eval_zh_answers.md` | Chinese RAG-only benchmark report. |
| `data/eval_zh_gpt_vs_rag.md` | Reusable GPT-only vs RAG baseline report. |
| `data/eval_zh_gpt_vs_rag_*.md` | Ad hoc GPT-only vs RAG benchmark reports. |
| `data/eval_en_faithfulness.md` | Claim-level faithfulness report for the English GPT-only vs RAG report. |
| `data/eval_en_faithfulness.json` | Machine-readable faithfulness details. |
| `data/eval_zh_web*.md` | Web-triggered benchmark reports. |
| `fulltext_xml/` | Downloaded PMC/JATS XML files. |

The current reusable snapshot files listed in “Checked-in Data Snapshot” are committed intentionally, even though they are generated from the pipeline.

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
