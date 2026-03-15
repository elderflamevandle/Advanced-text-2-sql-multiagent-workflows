"""BaseRetriever ABC with Pinecone and ChromaDB backends (VECTOR-001, VECTOR-003)."""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from vector.embeddings import EmbeddingGenerator, build_column_text, build_table_text
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
    """Unified interface for vector-based schema retrieval backends."""

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
        self._embedder = EmbeddingGenerator()
        self._schema_cache: dict | None = None

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
        if self.namespace_exists(namespace):
            logger.info(
                "Namespace %s already exists, skipping embedding", namespace
            )
            return

        self._schema_cache = schema

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
        if self._schema_cache is not None:
            graph = SchemaGraph(self._schema_cache)
            expanded = graph.expand_tables(matched_table_names)
            join_hints = graph.generate_join_hints(expanded)
        else:
            logger.warning(
                "schema_cache is None — FK expansion skipped for namespace '%s'",
                namespace,
            )
            expanded = matched_table_names
            join_hints = []

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
        self._embedder = EmbeddingGenerator()
        self._schema_cache: dict | None = None

    def _collection_name(self, namespace: str, tier: str) -> str:
        """Sanitize namespace for ChromaDB — colons become underscores."""
        return f"{namespace.replace(':', '_')}_{tier}"

    def namespace_exists(self, namespace: str) -> bool:
        existing = [c.name for c in self._client.list_collections()]
        return self._collection_name(namespace, "tables") in existing

    def embed_schema(self, schema: dict[str, Any], namespace: str) -> None:
        if self.namespace_exists(namespace):
            logger.info(
                "Namespace %s already exists, skipping embedding", namespace
            )
            return

        self._schema_cache = schema

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
        if self._schema_cache is not None:
            graph = SchemaGraph(self._schema_cache)
            expanded = graph.expand_tables(matched_table_names)
            join_hints = graph.generate_join_hints(expanded)
        else:
            logger.warning(
                "schema_cache is None — FK expansion skipped for namespace '%s'",
                namespace,
            )
            expanded = matched_table_names
            join_hints = []

        return {
            "tables": expanded,
            "table_metadata": column_metadata_list,
            "join_hints": join_hints,
            "scores": score_dict,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_retriever() -> BaseRetriever:
    """Return PineconeRetriever if PINECONE_API_KEY is set, else ChromaRetriever."""
    if os.getenv("PINECONE_API_KEY"):
        return PineconeRetriever()
    logger.warning(
        "PINECONE_API_KEY not set - using ChromaDB local fallback"
    )
    return ChromaRetriever()
