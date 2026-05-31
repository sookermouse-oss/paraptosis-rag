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

## Common commands

Run these from the project root:

```bash
./scripts/rag-build-500.sh
./scripts/rag-benchmark.sh
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
