"""Bounded retrieval for current, authorized recognition records.

The package deliberately keeps the recognition store authoritative.  Vectors
and ranks are derived at read time or in a replaceable index and never decide
whether a recognition revision is current.
"""

from .service import (
    EmbeddingProvider,
    EmbeddingCache,
    SQLiteEmbeddingCache,
    HttpEmbeddingProvider,
    HttpReranker,
    JsonHttpClient,
    RecognitionRetrievalError,
    Reranker,
    RetrievalHit,
    RetrievalResult,
    SqliteVecCandidateIndex,
    retrieve,
)

__all__ = [
    "EmbeddingProvider",
    "EmbeddingCache",
    "SQLiteEmbeddingCache",
    "HttpEmbeddingProvider",
    "HttpReranker",
    "JsonHttpClient",
    "RecognitionRetrievalError",
    "Reranker",
    "RetrievalHit",
    "RetrievalResult",
    "SqliteVecCandidateIndex",
    "retrieve",
]
