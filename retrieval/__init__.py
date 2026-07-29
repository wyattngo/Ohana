"""Vector retrieval boundary. `Retriever` interface + `PgvectorRetriever` (swap Qdrant later).

Namespace-scoping rule: ALL vector retrieval goes through `Retriever.search(namespaces=...)`;
`namespaces` required (no default); raw SQL on `embeddings` is FORBIDDEN outside this package.
Single choke-point so cross-tenant namespace leakage can't be introduced by forgetting a filter.
"""

from retrieval.base import Hit, Retriever

__all__ = ["Hit", "Retriever"]
