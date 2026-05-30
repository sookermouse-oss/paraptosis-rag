from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from search_nodes import DEFAULT_QUERIES, filter_nodes, load_nodes, search as bm25_search


DEFAULT_NODES_PATH = Path("data/nodes.jsonl")
DEFAULT_STORAGE_DIR = Path("data/llamaindex_storage")
DEFAULT_MODEL = "pritamdeka/S-PubMedBert-MS-MARCO"


def node_to_document(node: dict[str, Any]) -> Any:
    Document = llamaindex_imports()["Document"]
    return Document(
        id_=node["node_id"],
        text=node.get("text", ""),
        metadata=node_metadata(node),
    )


def node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node.get("node_id"),
        "pmcid": node.get("pmcid"),
        "pmid": node.get("pmid"),
        "doi": node.get("doi"),
        "title": node.get("title"),
        "year": node.get("year"),
        "journal": node.get("journal"),
        "keywords": node.get("keywords", []),
        "section_title": node.get("section_title"),
        "section_type": node.get("section_type"),
        "section_index": node.get("section_index"),
        "chunk_index": node.get("chunk_index"),
        "source_file": node.get("source_file"),
    }


def build_index(nodes_path: Path, storage_dir: Path, model_name: str) -> int:
    imports = llamaindex_imports()
    documents = [node_to_document(node) for node in load_nodes(nodes_path)]
    if not documents:
        raise SystemExit(f"No nodes found in {nodes_path}")

    embed_model = load_huggingface_embedding(model_name)
    imports["Settings"].embed_model = embed_model

    storage_context = imports["StorageContext"].from_defaults(
        vector_store=imports["SimpleVectorStore"]()
    )
    index = imports["VectorStoreIndex"].from_documents(
        documents,
        storage_context=storage_context,
        embed_model=embed_model,
        show_progress=True,
    )
    index.storage_context.persist(persist_dir=str(storage_dir))
    return len(documents)


def load_index(storage_dir: Path, model_name: str) -> Any:
    imports = llamaindex_imports()
    embed_model = load_huggingface_embedding(model_name)
    imports["Settings"].embed_model = embed_model
    storage_context = imports["StorageContext"].from_defaults(persist_dir=str(storage_dir))
    return imports["load_index_from_storage"](
        storage_context,
        embed_model=embed_model,
    )


def retrieve(
    index: Any,
    query: str,
    top_k: int,
    exclude_section_types: list[str],
) -> list[dict[str, Any]]:
    requested_top_k = top_k + len(exclude_section_types) * top_k
    retriever = index.as_retriever(similarity_top_k=max(top_k, requested_top_k))
    excluded = {section_type.casefold() for section_type in exclude_section_types}
    results: list[dict[str, Any]] = []

    for result in retriever.retrieve(query):
        metadata = result.node.metadata
        if (metadata.get("section_type") or "").casefold() in excluded:
            continue
        results.append(format_result(result.score or 0.0, metadata, result.node.get_content()))
        if len(results) >= top_k:
            break
    return results


def benchmark(
    nodes_path: Path,
    storage_dir: Path,
    model_name: str,
    top_k: int,
    exclude_section_types: list[str],
) -> None:
    nodes = filter_nodes(load_nodes(nodes_path), exclude_section_types)
    index = load_index(storage_dir, model_name)

    for query in DEFAULT_QUERIES_FOR_BENCHMARK:
        bm25_results = [
            format_result(score, node_metadata(node), node.get("text", ""))
            for score, node in bm25_search(nodes, query, top_k)
        ]
        llama_results = retrieve(index, query, top_k, exclude_section_types)
        print(f"\n=== {query} ===")
        print("BM25 section_type distribution:", dict(section_type_counts(bm25_results)))
        print("LlamaIndex section_type distribution:", dict(section_type_counts(llama_results)))
        print("BM25 Top 10:")
        for result in bm25_results:
            print(json.dumps(result, ensure_ascii=False))
        print("LlamaIndex Top 10:")
        for result in llama_results:
            print(json.dumps(result, ensure_ascii=False))


DEFAULT_QUERIES_FOR_BENCHMARK = [
    "How does ER stress induce paraptosis?",
    "What is the role of PI4KB in paraptosis?",
    "paraptosis and calcium homeostasis",
    "paraptosis in breast cancer prognosis",
    "paraptosis drug resistance",
]


def section_type_counts(results: list[dict[str, Any]]) -> Counter[str]:
    return Counter(result.get("section_type") or "missing" for result in results)


def format_result(score: float, metadata: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "score": round(score, 4),
        "title": metadata.get("title"),
        "section_title": metadata.get("section_title"),
        "section_type": metadata.get("section_type"),
        "node_id": metadata.get("node_id"),
        "preview": preview(text),
    }


def preview(text: str, max_chars: int = 300) -> str:
    return " ".join(text.split())[:max_chars].rstrip()


def load_huggingface_embedding(model_name: str) -> Any:
    imports = llamaindex_imports()
    try:
        return imports["HuggingFaceEmbedding"](model_name=model_name)
    except Exception as exc:
        raise SystemExit(
            f"Failed to load HuggingFace embedding model '{model_name}'. "
            "Install dependencies and ensure the model can be downloaded or is cached locally. "
            f"Original error: {exc}"
        ) from exc


def llamaindex_imports() -> dict[str, Any]:
    try:
        from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
        from llama_index.core import load_index_from_storage
        from llama_index.core.vector_stores import SimpleVectorStore
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    except ImportError as exc:
        raise SystemExit(
            "Missing LlamaIndex HuggingFace dependencies. Run `pip install -r requirements.txt` first."
        ) from exc

    return {
        "Document": Document,
        "Settings": Settings,
        "StorageContext": StorageContext,
        "VectorStoreIndex": VectorStoreIndex,
        "SimpleVectorStore": SimpleVectorStore,
        "load_index_from_storage": load_index_from_storage,
        "HuggingFaceEmbedding": HuggingFaceEmbedding,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local LlamaIndex retriever using HuggingFace biomedical embeddings and SimpleVectorStore."
    )
    parser.add_argument("command", choices=("build", "search", "benchmark"))
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES_PATH)
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE_DIR)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--query", action="append", help="Query to search. Can be passed multiple times.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--exclude-section-type",
        action="append",
        default=[],
        help="Section type to exclude, e.g. methods. Can be passed multiple times.",
    )
    args = parser.parse_args()

    if args.command == "build":
        count = build_index(args.nodes, args.storage_dir, args.model)
        print(f"Indexed documents: {count}")
        print(f"Storage directory: {args.storage_dir}")
        return

    if args.command == "benchmark":
        benchmark(args.nodes, args.storage_dir, args.model, args.top_k, args.exclude_section_type)
        return

    index = load_index(args.storage_dir, args.model)
    queries = args.query or DEFAULT_QUERIES
    for query in queries:
        print(f"\n=== {query} ===")
        for result in retrieve(index, query, args.top_k, args.exclude_section_type):
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
