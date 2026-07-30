"""Dev-only fallback embedder — chuyển từ `api/admin.py` sang đây ở fix I5b.

Chỉ được `agent.embedder.default_embedder()` chọn khi KHÔNG có key nào và
`OHANA_ENV=dev`. Kể từ spec 05 P1, adapter thật (`TogetherEmbedder`/`OpenAIEmbedder`)
là mặc định mỗi khi có key — class này là fallback không-key cho dev, không phải
default vô điều kiện.

Vector hash deterministic, cùng shape `FakeEmbedder` của `tests/test_wiki_rag.py`
dùng để gate vòng ingest/search mà không cần mạng. Nó tập luyện contract storage/HTTP
thật (số chunk, DB row) — KHÔNG đại diện chất lượng semantic search;
`tests/test_wiki_rag_live.py` (`@pytest.mark.live`) mới là acceptance check
embedding thật (ISSUE-016).

Từ chối chạy ngoài dev (xem `embed`) — defense in depth: kể cả khi logic chọn ở
factory có ngày chọn nhầm class này ngoài dev, chính `embed()` vẫn từ chối.
Docstring ghi "placeholder" không làm placeholder an toàn — cùng lập luận với
dev fallback của `auth.identity.get_jwt_secret` gate trên `OHANA_ENV`.
"""

from __future__ import annotations

import os

from agent.embedder import Embedder
from app.config import EMBED_DIM

# Alias tới nguồn sự thật duy nhất, KHÔNG phải bản sao — xem lịch sử ở spec 08 E1:
# hằng 1536 viết cứng đã lệch khỏi cột DB khi cột đổi sang 1024. Import thì máy giữ hộ.
_DEV_EMBED_DIM = EMBED_DIM


class DeterministicDevEmbedder(Embedder):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Fail LOUD outside dev rather than returning hash vectors that look like embeddings.
        # The failure mode this closes is silent-wrong, not crash: ingest would answer
        # {"success": true, "chunks": N} while writing semantically meaningless vectors, so
        # `search_wiki` would then feed near-random chunks to the drafter and the AI would
        # answer a customer confidently from the wrong source. Nobody sees a stack trace; they
        # see a plausible wrong reply. Raising here (not in `default_embedder()` — see that
        # function's docstring) keeps `app/main.py` importable (P0/P1 screens stay up) and
        # confines the blast radius to this one route.
        if os.environ.get("OHANA_ENV") != "dev":
            raise RuntimeError(
                "Wiki ingest is unavailable: no real embedder is configured "
                "(no OPENAI_API_KEY outside dev). Refusing to write placeholder vectors "
                "outside dev — they would silently corrupt wiki-RAG answers."
            )
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * _DEV_EMBED_DIM
            for tok in t.lower().split():
                vec[hash(tok) % _DEV_EMBED_DIM] = 1.0
            out.append(vec)
        return out
