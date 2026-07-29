"""Together AI adapter cho `Embedder` — e5 multilingual (spec 08 §7 Phase E0).

Together phục vụ embedding qua wire format tương thích OpenAI, nên đường SDK dùng lại y hệt
`TogetherClient`: `AsyncOpenAI(base_url=…).embeddings.create(…)`.

**Vì sao KHÔNG subclass `OpenAIEmbedder`** (khác `TogetherClient` subclass `OpenAIClient`):
class này phải override CẢ HAI method để gắn prefix, tức phần thân không còn gì dùng lại
ngoài 2 dòng gọi SDK. Subclass ở đây chỉ tạo ràng buộc kế thừa giả — đổi `OpenAIEmbedder`
sau này sẽ kéo theo hành vi ngoài ý muốn ở đây.

**Prefix `query: ` / `passage: ` — đây là toàn bộ lý do class này tồn tại.** e5 là model
BẤT ĐỐI XỨNG: nó được huấn luyện để nhúng câu hỏi và đoạn văn vào hai vai khác nhau, và
phân biệt hai vai bằng đúng chuỗi prefix này. Đo thật (spec 08 §5.5) trên một cặp mẫu tiếng
Việt: biên phân tách +0.1249 có prefix vs +0.1101 không prefix.

Rủi ro thật KHÔNG phải "quên cả hai" — mà là **BẤT ĐỐI XỨNG**: corpus embed có prefix còn
query thì không (hoặc ngược lại). Ca đó không crash, không sai type, không đỏ test thường;
nó chỉ khiến chunk trả về kém liên quan → AI trả lời khách bằng căn cứ sai, không stack
trace. Cùng họ failure với `_DeterministicDevEmbedder` (spec 04 P2): silent-wrong tệ hơn
crash. Vì vậy `tests/test_together_embedder.py` có gate riêng canh đúng ca hoán đổi.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from agent.embedder import Embedder
from app.config import DEFAULT_TOGETHER_EMBED_MODEL, get_settings

# Endpoint OpenAI-compatible của Together. Hằng số module, KHÔNG phải env và KHÔNG nhận
# override qua `__init__` — cùng lý do đã viết trong `together_client.py`: nhận override sẽ
# biến class thành "embedder trỏ đâu cũng được" và làm cái tên nói dối. Muốn provider khác
# thì thêm adapter khác, đừng trỏ lại cái này.
TOGETHER_BASE_URL = "https://api.together.xyz/v1"

_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "


def _prefixed(prefix: str, text: str) -> str:
    """Gắn prefix, idempotent — text đã mang đúng prefix thì giữ nguyên.

    `query: query: phí ship` là input rác: e5 sẽ coi chuỗi prefix thứ hai như một phần nội
    dung. Idempotent ở đây rẻ và chặn được ca call-site (hoặc một adapter bọc ngoài) vô tình
    gắn hai lần.
    """
    return text if text.startswith(prefix) else f"{prefix}{text}"


class TogetherEmbedder(Embedder):
    """`Embedder` trỏ Together/e5. Key + model đọc từ `Settings` (env), không hardcode."""

    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        settings = get_settings()

        # Model chốt HẲN ở đây, ba lớp, giống `TogetherClient` — và vì cùng một lý do đã
        # cháy thật: `TOGETHER_MODEL=` rỗng ⇒ falsy ⇒ trượt `or` ⇒ client trỏ Together
        # nhưng xin model của provider khác ⇒ 404. Mọi test khi đó vẫn xanh vì dùng fake
        # client. Ở đường embedding hậu quả còn tệ hơn đường chat: sai model = sai SỐ CHIỀU,
        # và dim mismatch chỉ lộ khi Postgres từ chối insert — sau khi đã embed cả corpus.
        #
        # `.strip()` chứ không chỉ kiểm falsy: `"  "` truthy nhưng là model id vô nghĩa.
        #
        # Lưu ý `_blank_env_means_unset` trong `app/config.py` KHÔNG phủ được mọi kiểu field
        # (ISSUE-018) — nên ba lớp này là phòng thủ thật, không phải trùng lặp thừa.
        self._model = (
            (model or "").strip()
            or (settings.together_embed_model or "").strip()
            or DEFAULT_TOGETHER_EMBED_MODEL
        )

        self._client = client or AsyncOpenAI(
            api_key=api_key or settings.together_api_key,
            base_url=TOGETHER_BASE_URL,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed THÔ, không prefix.

        Có mặt để thoả ABC. Call-site KHÔNG nên gọi thẳng cái này với e5 — không prefix
        nghĩa là không khai vai, và e5 sẽ nhúng nhầm không gian. Dùng `embed_query` /
        `embed_documents`.
        """
        resp = await self._client.embeddings.create(model=self._model, input=texts)
        return [item.embedding for item in resp.data]

    async def embed_query(self, text: str) -> list[float]:
        (vec,) = await self.embed([_prefixed(_QUERY_PREFIX, text)])
        return vec

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embed([_prefixed(_PASSAGE_PREFIX, t) for t in texts])
