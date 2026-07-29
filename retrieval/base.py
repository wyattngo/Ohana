"""Retriever interface. `namespaces` is REQUIRED (no default) — caller MUST scope."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Hit:
    namespace: str
    source_ref: str | None
    chunk: str
    score: float  # cosine distance (smaller = nearer)


class Retriever(ABC):
    @abstractmethod
    async def search(self, namespaces: list[str], query: list[float], k: int) -> list[Hit]:
        """k nearest chunks RESTRICTED to `namespaces` (SQL-level filter, không post-filter).

        `namespaces` bắt buộc, không default → không thể quên scope. Rỗng namespaces → rỗng kết quả.
        Cross-namespace leak bị cấm.
        """
        ...
