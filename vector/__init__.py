"""Vector package: semantic schema retrieval with Pinecone/ChromaDB backends."""
from vector.retriever import BaseRetriever, get_retriever
from vector.embeddings import EmbeddingGenerator, build_table_text, build_column_text
from vector.schema_graph import SchemaGraph

__all__ = [
    "BaseRetriever",
    "get_retriever",
    "EmbeddingGenerator",
    "build_table_text",
    "build_column_text",
    "SchemaGraph",
]
