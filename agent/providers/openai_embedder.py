"""OpenAI-compatible embedding adapter. Dim must match the pgvector `embeddings.embedding` column
(set to the provider's model dim at Alembic migration time)."""

from __future__ import annotations

from openai import AsyncOpenAI

from agent.embedder import Embedder
from app.config import get_settings


class OpenAIEmbedder(Embedder):
    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        settings = get_settings()
        # Same gateway seam as OpenAIClient — embeddings route through the proxy when configured;
        # base_url/api_key None → direct OpenAI (behavior-preserving).
        self._client = client or AsyncOpenAI(
            api_key=api_key or settings.openai_api_key, base_url=base_url
        )
        self._model = model or settings.openai_embed_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(model=self._model, input=texts)
        return [item.embedding for item in resp.data]
