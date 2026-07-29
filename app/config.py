"""Settings foundation (spec `05-Task-OhanaAISeller-ConfigEmbedder-F1.md` §7 Phase P0).

Exists so `agent/providers/openai_embedder.py` and `agent/providers/openai_client.py` — both
ported from `drnickv4/` doing `from app.config import get_settings` — resolve. Before this
module existed, `OpenAIEmbedder()` raised `ModuleNotFoundError` (ISSUE-016 root cause), which
is why F1 wiki-RAG was never verified against a real embedder despite being tick DONE.

Scope is P0 only: the 4 fields the two providers reference. Wiring `OpenAIEmbedder` into the
live `default_embedder()` factory (`api/admin.py`) is Phase P1, not here.

`openai_model` and `reasoning_models` get defaults (rather than being required) so
`get_settings()` works with zero env configured — the dev-without-key path P0 exists to keep
alive. Neither field is exercised by any live code path yet (the LLM client isn't wired into
`app/main.py`); the defaults only need to let `OpenAIClient` import + instantiate without
raising, not represent a real deployment choice.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Model Together mặc định — Wyatt ký lại PRE-G02 ngày 2026-07-19. Hằng số CÓ TÊN chứ không
# phải literal rải rác, vì `agent/providers/together_client.py` cần đúng giá trị này làm chốt
# chặn cuối; hai bản sao literal sẽ lệch nhau vào một ngày nào đó.
#
# Lựa chọn đầu (`Qwen/Qwen2.5-72B-Instruct-Turbo`) KHÔNG dùng được: Together liệt kê nó trong
# `/v1/models` KÈM bảng giá, nhưng gọi thật trả 400 "non-serverless — cần dedicated endpoint".
# Danh sách model không phải bằng chứng về khả năng phục vụ; chỉ một cuộc gọi thật mới là.
# Dò 148 ứng viên chat ⇒ đúng 6 cái chạy được (spec 07 §14).
#
# Chọn non-reasoning có chủ đích: model reasoning (gpt-oss, GLM, Kimi) trả `content` RỖNG khi
# `max_tokens` không đủ chỗ cho phần suy luận — hỏng âm thầm, không exception. Llama-3.3-70B
# không mang lớp rủi ro đó, không bịa số liệu trong test, và tiếng Việt tự nhiên đúng ngữ vực
# bán hàng. Vẫn là open-weight ⇒ lập luận portability của ADR PRE-007 giữ nguyên.
DEFAULT_TOGETHER_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

# Embedding model mặc định — ADR PRE-007 (ACCEPTED 2026-07-19) chốt e5 open-weight thay
# `text-embedding-3-small` của OpenAI. Không lấy từ bảng giá hay `/v1/models`: spec 08 §5.1
# gọi thật `POST /v1/embeddings` → HTTP 200, dimension THẬT = 1024. (Bài học spec 07: model
# có trong danh sách kèm giá vẫn có thể trả 400 "non-serverless" — danh sách không phải
# bằng chứng.)
DEFAULT_TOGETHER_EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"

# Số chiều vector — NGUỒN SỰ THẬT DUY NHẤT. `db/models.py` import hằng số này thay vì khai
# một số riêng; hai hằng số độc lập cùng mô tả một thực tế vật lý (số cột trong Postgres) là
# cách chắc chắn nhất để một ngày chúng lệch nhau, và khi lệch thì triệu chứng là insert bị
# từ chối ở một đường code còn đường khác vẫn chạy.
#
# **HẰNG SỐ MODULE, CỐ Ý KHÔNG PHẢI field của `Settings`** (Wyatt duyệt 2026-07-19, spec 08 E1
# DEVIATION_APPROVED). `Settings` đọc env ⇒ để nó thành field env là cho phép biến môi trường
# điều khiển SỐ CHIỀU CỘT DB, trong khi cột thật do migration cố định. Đặt sai env thì hoặc
# insert bị từ chối hàng loạt, hoặc — tệ hơn — hai đường code lệch nhau âm thầm. Schema không
# phải thứ để cấu hình lúc chạy.
#
# 1024 = đo THẬT, không đọc doc: E0 gọi e5 qua mạng và đếm (`docs/smokes/08-E0.md` R1).
# Đổi số này là BREAKING — cần Alembic migration đổi `Vector(...)` + re-embed toàn bộ corpus.
EMBED_DIM = 1024


class Settings(BaseSettings):
    # No `env_file` — read from process env only, matching how every other env-reader in this
    # repo works (`db/session.py get_database_url`, `auth/identity.py get_jwt_secret`, both
    # `os.environ.get(...)` directly). A `.env`-file source would read a local dev file even
    # after `monkeypatch.delenv` clears `os.environ`, silently reintroducing the exact
    # stale-state trap this spec exists to close (see module docstring + P0 test docstring).
    model_config = SettingsConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _blank_env_means_unset(cls, data: Any) -> Any:
        """Biến môi trường khai báo nhưng RỖNG ⇒ coi như KHÔNG khai báo (default được dùng).

        Mặc định của pydantic-settings là coi `FOO=` như giá trị hợp lệ `""`, ghi đè default.
        Điều đó đúng về mặt kỹ thuật và sai về mặt vận hành: `.env.example` liệt kê tên biến
        với giá trị trống để admin điền, ai copy thành `.env` mà chưa điền hết sẽ *tắt* default
        thay vì *nhận* default.

        Đã cháy thật (spec 07 G1 smoke, 2026-07-19): `TOGETHER_MODEL=` rỗng ⇒ `together_model`
        = `""` ⇒ chuỗi rỗng falsy trượt qua các `or` phía dưới ⇒ `TogetherClient` xin
        `gpt-4o-mini` từ Together ⇒ 404. Toàn bộ 90 test vẫn xanh vì test dùng fake client,
        không chạm model id thật.

        PHẠM VI — chỉ field VÔ HƯỚNG (`str`, `str | None`). Đó là nơi cái bẫy đang chờ:
        `DATABASE_URL`, `OHANA_JWT_SECRET`, `OPENAI_MODEL`. Với `str | None` thì bỏ key đi
        cũng ra `None` như cũ — không đổi hành vi; với `str` có default thì đây chính là
        phần vá.

        ⚠️ Hàm này CHỈ phủ field VÔ HƯỚNG. Với field kiểu PHỨC (`frozenset[str]`, list, dict)
        nó chạy `mode="before"` = *sau* khi `EnvSettingsSource` đã thử JSON-parse env, nên
        chuỗi rỗng nổ tại tầng source trước khi validator thấy — từng là ISSUE-018:

            REASONING_MODELS=  ⇒  SettingsError: error parsing value for field
                                  "reasoning_models" from source "EnvSettingsSource"

        Cách đóng (2026-07-21): field phức nào cần "rỗng = chưa set" phải mang `NoDecode` để
        tắt parse ở tầng source (giá trị tới đây dưới dạng chuỗi thô) + một `field_validator`
        riêng biến chuỗi thành collection. `reasoning_models` — field phức DUY NHẤT hiện có —
        đã làm vậy ngay dưới đây, nên chuỗi thô "" lọt tới dict và bị chính hàm này coi là
        chưa set. Thêm field phức thứ hai: lặp lại đúng cặp `NoDecode` + validator, đừng
        trông vào hàm này một mình.

        Đã kiểm tay trên path bảo mật (`get_jwt_secret`), fail-closed giữ nguyên: unset /
        rỗng / production đều RAISE, secret thật vẫn OK, dev vẫn fallback. Có ĐÚNG MỘT thay
        đổi hành vi, và nó theo hướng an toàn hơn: `OHANA_JWT_SECRET="   "` (chỉ khoảng
        trắng) trước đây là truthy nên được dùng LÀM SECRET THẬT; giờ nó bị coi như chưa set
        ⇒ raise ngoài dev. Không ai cố tình đặt secret bằng khoảng trắng — nhưng copy/paste
        hỏng thì có.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and v.strip() == "")}
        return data

    # None allowed — dev-without-key path (DoD #1). Read from env `OPENAI_API_KEY`
    # (pydantic-settings v2 is case-insensitive by default; verified empirically in
    # tests/test_config.py::test_openai_api_key_reads_from_env, not just assumed from docs).
    openai_api_key: str | None = None

    # ⚠️ 1536 dims — KHÔNG còn khớp cột DB kể từ spec 08 E1 (cột giờ là `Vector(EMBED_DIM)` =
    # 1024). `OpenAIEmbedder` được GIỮ có chủ ý làm adapter thay thế (spec 08 §3 "Out of
    # scope"), nhưng dùng nó để ingest vào schema hiện tại sẽ bị Postgres TỪ CHỐI vì sai chiều
    # — ồn ào, không âm thầm. Muốn quay lại OpenAI thì cần migration đổi `EMBED_DIM` + re-embed,
    # không phải chỉ đổi dòng này.
    openai_embed_model: str = "text-embedding-3-small"

    # `agent/providers/openai_client.py OpenAIClient.__init__` needs this field to exist so the
    # import + instantiation don't raise, even though the client isn't wired into any live path
    # yet (P0 scope is "imports resolve", not "chat client works"). Default is a real, current
    # OpenAI chat model string rather than a placeholder — an unwired-but-importable client
    # should not carry a fake model id that would silently 404 if someone reached for it before
    # the real wiring phase lands.
    openai_model: str = "gpt-4o-mini"

    # `agent/providers/openai_client.py:252` does `model in get_settings().reasoning_models`
    # (membership test) — frozenset gives that O(1) with immutable-by-default semantics. Empty
    # by default: no model is in "reasoning mode" until a future phase wires the client and
    # deliberately opts models in.
    #
    # `NoDecode` tắt JSON-parse của EnvSettingsSource cho field này (ISSUE-018): env tới dưới
    # dạng chuỗi thô, nên `REASONING_MODELS=` rỗng không còn nổ `SettingsError` ở tầng source
    # — nó lọt tới `_blank_env_means_unset` và được coi như chưa set. `_parse_reasoning_models`
    # dưới đây nhận chuỗi thô khi env được set và tách theo dấu phẩy thành frozenset.
    reasoning_models: Annotated[frozenset[str], NoDecode] = frozenset()

    @field_validator("reasoning_models", mode="before")
    @classmethod
    def _parse_reasoning_models(cls, v: Any) -> Any:
        """Chuỗi env thô (do `NoDecode`) → `frozenset`; rỗng/khoảng trắng → `frozenset()`.

        Chỉ chạy khi env ĐƯỢC set (field validator không chạy trên default), nên giá trị vào
        là `str`. Khi env không set, default `frozenset()` dùng thẳng. Comma-separated cho
        khớp cách admin sẽ khai; phần tử rỗng bị loại để `"a,,b"` không sinh chuỗi rỗng.
        """
        if isinstance(v, str):
            return frozenset(part.strip() for part in v.split(",") if part.strip())
        return v

    # ---- G0 (spec 07 §7 Phase G0) — Together AI, provider cho General Chat.
    #
    # Together nói OpenAI-compatible wire format, nên `TogetherClient` chỉ là `OpenAIClient`
    # trỏ base_url khác — không nhân bản 380 dòng adapter.

    # `TOGETHER_API_KEY`. None khi chưa set — cùng dáng với `openai_api_key` ở trên. Giá trị
    # KHÔNG bao giờ được log/echo/trả về qua API.
    #
    # ⚠️ Đã kiểm bằng test, không phải suy đoán: thiếu key thì `TogetherClient()` ném
    # `openai.OpenAIError` ngay lúc DỰNG object, KHÔNG phải lúc gọi API (SDK openai validate
    # credentials trong `AsyncOpenAI.__init__`). Nghĩa là G1 **không được** dựng client ở
    # module scope của `app/main.py` — làm vậy thì deploy quên set key = app không boot nổi,
    # thay vì chỉ endpoint chat hỏng. Dựng lazy trong request handler / dependency.
    together_api_key: str | None = None

    # `TOGETHER_MODEL`. Default = model Wyatt ký ở spec 07 §14 (PRE-G02). Có default để đổi
    # model là sửa env chứ không sửa code — Roadmap §8.2 cấm hardcode model id ở call site.
    # Đổi default ở đây KHÔNG kéo theo migration nào (khác `openai_embed_model`): chat model
    # không đụng cột vector.
    together_model: str = DEFAULT_TOGETHER_MODEL

    # `TOGETHER_EMBED_MODEL`. Default = e5 chốt ở ADR PRE-007, đã verify bằng gọi thật
    # (spec 08 §5.1: POST /v1/embeddings → 200, dim THẬT = 1024).
    #
    # ⚠️ KHÁC `together_model` ở một điểm sống còn: đổi default Ở ĐÂY **KÉO THEO MIGRATION**.
    # Model embedding quyết định số chiều của cột `embeddings.embedding`; đổi model mà không
    # đổi `Vector(...)` thì Postgres từ chối insert (ồn ào, an toàn) — nhưng đổi cả hai mà
    # không re-embed thì corpus cũ nằm sai không gian và retrieval hỏng ÂM THẦM. Xem
    # `db/models.py _EMBED_DIM` + spec 08 §8 trước khi chạm dòng này.
    together_embed_model: str = DEFAULT_TOGETHER_EMBED_MODEL

    # ---- P2 (spec 05 §7 Phase P2) — consolidate the remaining direct `os.environ.get(...)`
    # reads into this one Settings surface. Pure refactor: these three fields exist so
    # `auth/identity.py get_jwt_secret()` and `db/session.py get_database_url()` have
    # somewhere to source from — they do NOT change what either function returns for a given
    # env. Both functions build a FRESH `Settings()` per call rather than going through the
    # `@lru_cache`d `get_settings()` below; see `get_jwt_secret()`'s docstring for why routing
    # a security-relevant read through the process-wide cache is unsafe (stale env across
    # tests/requests) — that reasoning is why these three fields are listed here but never
    # read via `get_settings()` anywhere in the codebase.

    # `OHANA_JWT_SECRET`. None when unset — absence is a real, meaningful state that
    # `get_jwt_secret()` branches on (dev literal fallback vs raise), not a value to paper
    # over with a default.
    ohana_jwt_secret: str | None = None

    # `OHANA_ENV`. None when unset (matches the pre-P2 `os.environ.get("OHANA_ENV")` shape,
    # which also yielded `None`, not `"production"` or any other literal). Every caller
    # already treats "anything other than exactly the string 'dev'" as non-dev, so `None`
    # compares equal to that "not dev" bucket without needing its own default.
    ohana_env: str | None = None

    # `DATABASE_URL`. Literal default matches `db/session.py`'s pre-P2 `_DEFAULT_URL` exactly
    # — local dev Postgres, same `ohana`/`ohana`/`ohana` used throughout
    # `docs/tasks/01-Task-OhanaAISeller-GD0.md` pre-flight checks. Unlike the two fields
    # above, pydantic-settings' own default mechanism reproduces
    # `os.environ.get("DATABASE_URL", _DEFAULT_URL)` exactly — no `is None` branch needed in
    # the caller.
    database_url: str = "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"


@lru_cache
def get_settings() -> Settings:
    return Settings()
