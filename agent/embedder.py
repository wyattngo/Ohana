"""Provider-agnostic embedding interface (tách khỏi LLMClient: embeddings ≠ chat).

Đổi provider (OpenAI → khác) = thêm adapter, KHÔNG sửa retrieval/agent core (§5.2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Embedder(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Trả vector mỗi text (đúng thứ tự). Dim khớp column Embedding.embedding."""
        ...

    # --- query/passage split (spec 08 §7 Phase E0) --------------------------------
    #
    # Hai method dưới đây là CONCRETE, không phải `@abstractmethod`, và đó là quyết định
    # có chủ ý: thêm abstract sẽ phá MỌI impl đang tồn tại (`OpenAIEmbedder`,
    # `_DeterministicDevEmbedder`, mọi fake trong tests) — chúng chỉ implement `embed()`.
    # Default là delegate NGUYÊN VĂN về `embed()`, nên adapter cũ giữ đúng hành vi cũ.
    #
    # Vì sao ABC không tự gắn prefix: prefix là đặc tính của MỘT HỌ MODEL (e5), không phải
    # của khái niệm "embedding". OpenAI `text-embedding-3-*` không dùng prefix — gắn ở tầng
    # chung sẽ làm bẩn mọi provider khác và không có chỗ nào tắt đi. Nên prefix thuộc về
    # ADAPTER (`TogetherEmbedder`), còn call-site chỉ khai *ý định*: đây là query hay corpus.
    #
    # Vì sao call-site phải khai ý định thay vì gọi thẳng `embed()`: e5 bất đối xứng — cùng
    # một câu, embed dạng query và dạng passage cho vector khác nhau. Trộn hai bên KHÔNG
    # crash và KHÔNG sai type; nó chỉ làm retrieval tệ đi âm thầm. `tools/wiki.py` (query)
    # và `parsing/ingest.py` (corpus) trước đây gọi cùng một `embed()`, tức không có chỗ
    # nào trong hệ thống biết mình đang ở bên nào — đó là lỗ hổng thiết kế spec 08 §5.4 nêu.

    async def embed_query(self, text: str) -> list[float]:
        """Embed MỘT câu truy vấn của người dùng.

        Default: không prefix, delegate về `embed()`. Adapter nào cần đánh dấu bên query
        (e5) thì override.
        """
        (vec,) = await self.embed([text])
        return vec

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed các đoạn văn bản của CORPUS (chunk wiki, mô tả sản phẩm…).

        Default: không prefix, delegate về `embed()`. Adapter nào cần đánh dấu bên passage
        (e5) thì override.
        """
        return await self.embed(texts)
