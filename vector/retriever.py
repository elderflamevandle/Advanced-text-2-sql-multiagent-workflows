"""BaseRetriever ABC with Pinecone and ChromaDB backends (VECTOR-001, VECTOR-003)."""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml

from vector.embeddings import (  # noqa: F401 - EmbeddingGenerator is re-exported
    EmbeddingGenerator,
    build_column_text,
    build_table_text,
    get_embedder,
)
from vector.namespace import schema_namespace  # noqa: F401 - re-exported for callers
from vector.schema_graph import SchemaGraph

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resolve path to pinecone_config.yaml relative to this file's package root
# ---------------------------------------------------------------------------
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "pinecone_config.yaml"


def _load_pinecone_config() -> dict:
    with open(_CONFIG_PATH, "r") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseRetriever(ABC):
    """Unified interface for vector-based schema retrieval backends.

    One retriever instance serves every database in the process (see ``get_retriever``),
    so nothing here may be specific to a single schema: the schemas used for FK expansion
    are kept per namespace.
    """

    # Schemas kept in memory for FK expansion; the least recently used are dropped first.
    MAX_CACHED_SCHEMAS = 256

    def _init_namespace_state(self) -> None:
        self._ns_lock = threading.Lock()
        self._schemas: OrderedDict[str, dict] = OrderedDict()
        self._embedded_namespaces: set[str] = set()

    def register_schema(self, namespace: str, schema: dict[str, Any]) -> None:
        """Remember *schema* for FK expansion and JOIN hints of *namespace*."""
        with self._ns_lock:
            self._schemas[namespace] = schema
            self._schemas.move_to_end(namespace)
            while len(self._schemas) > self.MAX_CACHED_SCHEMAS:
                self._schemas.popitem(last=False)

    def schema_for(self, namespace: str) -> dict[str, Any] | None:
        with self._ns_lock:
            schema = self._schemas.get(namespace)
            if schema is not None:
                self._schemas.move_to_end(namespace)
            return schema

    def _already_embedded(self, namespace: str) -> bool:
        """True when *namespace* has vectors; the store is asked once per namespace."""
        with self._ns_lock:
            if namespace in self._embedded_namespaces:
                return True
        if self.namespace_exists(namespace):
            self._mark_embedded(namespace)
            return True
        return False

    def _mark_embedded(self, namespace: str) -> None:
        with self._ns_lock:
            self._embedded_namespaces.add(namespace)

    def _expand(self, matched: list[str], namespace: str) -> tuple[list[str], list[dict]]:
        """FK-expand *matched* and build JOIN hints from the schema of *namespace*."""
        schema = self.schema_for(namespace)
        if schema is None:
            logger.warning(
                "No schema registered for namespace '%s': FK expansion skipped", namespace
            )
            return matched, []
        # Vectors can outlive a table (the store is shared and eventually consistent).
        known = [t for t in matched if t in schema]
        graph = SchemaGraph(schema)
        expanded = graph.expand_tables(known)
        return expanded, graph.generate_join_hints(expanded)

    @abstractmethod
    def embed_schema(self, schema: dict[str, Any], namespace: str) -> None:
        """Embed all tables/columns of *schema* into the vector store under *namespace*."""

    @abstractmethod
    def retrieve_tables(
        self, query: str, namespace: str, top_k: int = 5
    ) -> dict:
        """
        Retrieve relevant tables for *query*.

        Returns a dict with:
            tables         – list[str] of table name strings (FK-expanded)
            table_metadata – list[dict] with column-level metadata
            join_hints     – list[dict] from SchemaGraph.generate_join_hints()
            scores         – dict[table_name, float] relevance scores
        """

    @abstractmethod
    def namespace_exists(self, namespace: str) -> bool:
        """Return True when *namespace* already has embeddings in the store."""


# ---------------------------------------------------------------------------
# Pinecone backend
# ---------------------------------------------------------------------------


class PineconeRetriever(BaseRetriever):
    """Pinecone serverless index backend with two-stage table/column retrieval."""

    def __init__(self) -> None:
        # Lazy import — pinecone is an optional extra
        from pinecone import Pinecone, ServerlessSpec  # type: ignore[import]

        self._ServerlessSpec = ServerlessSpec
        self._config = _load_pinecone_config()
        self._pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        self._ensure_index()
        self._index = self._pc.Index(self._config["index_name"])
        self._embedder = get_embedder()
        self._init_namespace_state()

    def _ensure_index(self) -> None:
        """Create the serverless index if it doesn't yet exist."""
        cfg = self._config
        if not self._pc.has_index(cfg["index_name"]):
            logger.info("Creating Pinecone index '%s'...", cfg["index_name"])
            self._pc.create_index(
                name=cfg["index_name"],
                dimension=cfg["dimension"],
                metric=cfg["metric"],
                spec=self._ServerlessSpec(
                    cloud=cfg["cloud"],
                    region=cfg["region"],
                ),
            )

    def namespace_exists(self, namespace: str) -> bool:
        stats = self._index.describe_index_stats()
        return f"{namespace}:tables" in stats.get("namespaces", {})

    def embed_schema(self, schema: dict[str, Any], namespace: str) -> None:
        # Always remember the schema, also when the vectors already exist (after a
        # restart they do, and FK expansion still needs the schema).
        self.register_schema(namespace, schema)
        if self._already_embedded(namespace):
            logger.debug("Namespace %s already embedded, skipping", namespace)
            return

        table_names: list[str] = []
        table_texts: list[str] = []
        column_ids: list[str] = []
        column_texts: list[str] = []
        column_metadatas: list[dict] = []

        for table_name, table_data in schema.items():
            table_names.append(table_name)
            table_texts.append(build_table_text(table_name, table_data))

            sample_rows = table_data.get("sample_rows", [])
            for col in table_data["columns"]:
                col_name = col["name"]
                col_type = col["type"]
                # Extract sample values for this column from sample_rows
                sample_vals = [row.get(col_name) for row in sample_rows if col_name in row]
                column_ids.append(f"{namespace}::{table_name}::{col_name}")
                column_texts.append(
                    build_column_text(col_name, col_type, table_name, sample_vals)
                )
                column_metadatas.append(
                    {
                        "table_name": table_name,
                        "column_name": col_name,
                        "data_type": col_type,
                        "sample_values": str(sample_vals[:3]),
                    }
                )

        logger.info(
            "Embedding %d tables and %d columns for namespace '%s'...",
            len(table_names),
            len(column_ids),
            namespace,
        )
        table_embeddings = self._embedder.embed_documents(table_texts)
        column_embeddings = self._embedder.embed_documents(column_texts)

        # Upsert table vectors
        table_vectors = [
            {
                "id": f"{namespace}::{name}",
                "values": emb,
                "metadata": {"table_name": name, "text": txt},
            }
            for name, emb, txt in zip(table_names, table_embeddings, table_texts)
        ]
        self._index.upsert(vectors=table_vectors, namespace=f"{namespace}:tables")

        # Upsert column vectors
        column_vectors = [
            {
                "id": cid,
                "values": emb,
                "metadata": meta,
            }
            for cid, emb, meta in zip(column_ids, column_embeddings, column_metadatas)
        ]
        self._index.upsert(vectors=column_vectors, namespace=f"{namespace}:columns")
        self._mark_embedded(namespace)

    def retrieve_tables(
        self, query: str, namespace: str, top_k: int = 5
    ) -> dict:
        query_vec = list(self._embedder.embed_query_cached(query))

        # Stage 1: table-level retrieval
        table_results = self._index.query(
            vector=query_vec,
            top_k=top_k,
            namespace=f"{namespace}:tables",
            include_metadata=True,
        )
        matched_table_names: list[str] = [
            m["metadata"]["table_name"]
            for m in table_results.get("matches", [])
        ]
        score_dict: dict[str, float] = {
            m["metadata"]["table_name"]: m["score"]
            for m in table_results.get("matches", [])
        }

        # Stage 2: column-level retrieval filtered to matched tables
        column_results = self._index.query(
            vector=query_vec,
            top_k=top_k * 10,
            namespace=f"{namespace}:columns",
            include_metadata=True,
            filter={"table_name": {"$in": matched_table_names}},
        )
        column_metadata_list: list[dict] = [
            m["metadata"] for m in column_results.get("matches", [])
        ]

        # FK expansion and JOIN hints
        expanded, join_hints = self._expand(matched_table_names, namespace)

        return {
            "tables": expanded,
            "table_metadata": column_metadata_list,
            "join_hints": join_hints,
            "scores": score_dict,
        }


# ---------------------------------------------------------------------------
# ChromaDB backend
# ---------------------------------------------------------------------------


class ChromaRetriever(BaseRetriever):
    """Local ChromaDB fallback backend using PersistentClient."""

    def __init__(self) -> None:
        # Lazy import — chromadb is an optional extra
        import chromadb  # type: ignore[import]

        chroma_path = Path.home() / ".text2sql" / "chroma"
        chroma_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(chroma_path))
        self._embedder = get_embedder()
        self._init_namespace_state()

    def _collection_name(self, namespace: str, tier: str) -> str:
        """Sanitize namespace for ChromaDB — colons become underscores."""
        return f"{namespace.replace(':', '_')}_{tier}"

    def namespace_exists(self, namespace: str) -> bool:
        existing = [c.name for c in self._client.list_collections()]
        return self._collection_name(namespace, "tables") in existing

    def embed_schema(self, schema: dict[str, Any], namespace: str) -> None:
        # Always remember the schema, also when the vectors already exist (after a
        # restart they do, and FK expansion still needs the schema).
        self.register_schema(namespace, schema)
        if self._already_embedded(namespace):
            logger.debug("Namespace %s already embedded, skipping", namespace)
            return

        table_col = self._client.get_or_create_collection(
            self._collection_name(namespace, "tables")
        )
        col_col = self._client.get_or_create_collection(
            self._collection_name(namespace, "columns")
        )

        table_ids: list[str] = []
        table_texts: list[str] = []
        table_metadatas: list[dict] = []
        column_ids: list[str] = []
        column_texts: list[str] = []
        column_metadatas: list[dict] = []

        for table_name, table_data in schema.items():
            table_ids.append(f"{namespace}::{table_name}")
            table_texts.append(build_table_text(table_name, table_data))
            table_metadatas.append({"table_name": table_name})

            sample_rows = table_data.get("sample_rows", [])
            for col in table_data["columns"]:
                col_name = col["name"]
                col_type = col["type"]
                sample_vals = [row.get(col_name) for row in sample_rows if col_name in row]
                column_ids.append(f"{namespace}::{table_name}::{col_name}")
                column_texts.append(
                    build_column_text(col_name, col_type, table_name, sample_vals)
                )
                column_metadatas.append(
                    {
                        "table_name": table_name,
                        "column_name": col_name,
                        "data_type": col_type,
                        "sample_values": str(sample_vals[:3]),
                    }
                )

        logger.info(
            "Embedding %d tables and %d columns (ChromaDB) for namespace '%s'...",
            len(table_ids),
            len(column_ids),
            namespace,
        )
        table_embeddings = self._embedder.embed_documents(table_texts)
        column_embeddings = self._embedder.embed_documents(column_texts)

        table_col.upsert(
            ids=table_ids,
            embeddings=table_embeddings,
            metadatas=table_metadatas,
        )
        col_col.upsert(
            ids=column_ids,
            embeddings=column_embeddings,
            metadatas=column_metadatas,
        )
        self._mark_embedded(namespace)

    def retrieve_tables(
        self, query: str, namespace: str, top_k: int = 5
    ) -> dict:
        query_vec = list(self._embedder.embed_query_cached(query))

        table_col = self._client.get_or_create_collection(
            self._collection_name(namespace, "tables")
        )
        col_col = self._client.get_or_create_collection(
            self._collection_name(namespace, "columns")
        )

        # Stage 1: table-level
        table_results = table_col.query(
            query_embeddings=[query_vec],
            n_results=top_k,
            include=["metadatas", "distances"],
        )
        matched_table_names: list[str] = []
        score_dict: dict[str, float] = {}
        if table_results.get("metadatas") and table_results["metadatas"][0]:
            for meta, dist in zip(
                table_results["metadatas"][0],
                table_results.get("distances", [[]])[0],
            ):
                tname = meta["table_name"]
                matched_table_names.append(tname)
                score_dict[tname] = 1.0 - dist  # cosine distance -> similarity

        # Stage 2: column-level filtered to matched tables
        column_metadata_list: list[dict] = []
        if matched_table_names:
            col_results = col_col.query(
                query_embeddings=[query_vec],
                n_results=top_k * 10,
                where={"table_name": {"$in": matched_table_names}},
                include=["metadatas"],
            )
            if col_results.get("metadatas") and col_results["metadatas"][0]:
                column_metadata_list = col_results["metadatas"][0]

        # FK expansion and JOIN hints
        expanded, join_hints = self._expand(matched_table_names, namespace)

        return {
            "tables": expanded,
            "table_metadata": column_metadata_list,
            "join_hints": join_hints,
            "scores": score_dict,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_retrievers: dict[str, BaseRetriever] = {}
_retrievers_lock = threading.Lock()


def _backend_key() -> str:
    api_key = os.getenv("PINECONE_API_KEY")
    if api_key:
        # A different API key is a different Pinecone project, hence a different client.
        return "pinecone:" + hashlib.sha1(api_key.encode("utf-8")).hexdigest()[:12]
    return "chroma"


def get_retriever() -> BaseRetriever:
    """Return the shared retriever for the configured backend.

    PineconeRetriever if PINECONE_API_KEY is set, else ChromaRetriever. The instance is
    created once per backend and reused: building one opens a client and, on first use,
    loads the embedding model, which must not happen on every question.
    """
    key = _backend_key()
    retriever = _retrievers.get(key)
    if retriever is not None:
        return retriever
    with _retrievers_lock:
        retriever = _retrievers.get(key)
        if retriever is None:
            if key.startswith("pinecone:"):
                retriever = PineconeRetriever()
            else:
                logger.warning("PINECONE_API_KEY not set - using ChromaDB local fallback")
                retriever = ChromaRetriever()
            _retrievers[key] = retriever
    return retriever


def reset_retrievers() -> None:
    """Drop the cached retrievers (tests, or after changing vector-store settings)."""
    with _retrievers_lock:
        _retrievers.clear()
