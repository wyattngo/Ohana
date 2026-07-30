"""P0 gate (spec `05-Task-OhanaAISeller-ConfigEmbedder-F1.md` §7 Phase P0).

Written BEFORE `app/config.py` exists — expected RED (collection error: `ModuleNotFoundError:
No module named 'app.config'`) until the module lands. Locks the contract both
`agent/providers/openai_embedder.py` and `agent/providers/openai_client.py` need:
`Settings(BaseSettings)` + `get_settings()` exposing `openai_api_key`, `openai_embed_model`,
`openai_model`, `reasoning_models`.

`get_settings()` is `@lru_cache`d (singleton) — any test that mutates env MUST call
`get_settings.cache_clear()` first, else it silently asserts on a stale cached instance from
a prior test (a tautology that would pass even if env-reading were broken). Every test below
that touches env calls `cache_clear()` both before AND after (before: don't read a previous
test's cached instance; after: don't leak a monkeypatched instance to tests that run later in
the same process, since monkeypatch undoes the env var but not an already-cached Settings
object).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Every test starts and ends with a clean `get_settings()` cache — see module docstring."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_openai_embed_model_default_locked() -> None:
    """Q1 (spec 05 §14) locks `text-embedding-3-small` — 1536 dims.

    ⚠️ Cập nhật spec 08 E1: 1536 KHÔNG còn khớp cột DB. Cột giờ là `Vector(EMBED_DIM)` = 1024
    (Together e5). Test này vẫn đúng và vẫn cần: nó khoá DEFAULT của adapter OpenAI, thứ được
    giữ lại có chủ ý làm adapter thay thế. Nó KHÔNG còn khẳng định "khớp schema" — dùng
    `OpenAIEmbedder` để ingest vào schema hiện tại sẽ bị Postgres từ chối vì sai chiều (ồn ào,
    đúng ý muốn). Ràng buộc "một nguồn sự thật về số chiều" nay do
    `tests/test_embedding_dim.py` canh."""
    from app.config import get_settings

    assert get_settings().openai_embed_model == "text-embedding-3-small"


def test_openai_api_key_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev-without-key path (DoD #1) — must not raise, must be None (not empty string, not a
    placeholder literal)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    assert get_settings().openai_api_key is None


def test_openai_api_key_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empirical check that pydantic-settings actually maps `OPENAI_API_KEY` (env) ->
    `openai_api_key` (field) — do not trust the docs, prove it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    from app.config import get_settings

    get_settings.cache_clear()
    assert get_settings().openai_api_key == "sk-test-xyz"


def test_openai_embedder_imports_without_raising() -> None:
    """Proves `app.config` exists with a shape `OpenAIEmbedder` can import — this is the exact
    line that raised `ModuleNotFoundError` before P0 (ISSUE-016 root cause)."""
    from agent.providers.openai_embedder import OpenAIEmbedder  # noqa: F401


def test_settings_shape_satisfies_openai_client_needs() -> None:
    """Proves `openai_model` + `reasoning_models` are present on `Settings` with the shape
    `OpenAIClient` needs — `openai_model` used as `str` default-model, `reasoning_models` used
    as `model in get_settings().reasoning_models` (openai_client.py:118,252).

    NOT `from agent.providers.openai_client import OpenAIClient` directly — that import is
    blocked by a SEPARATE, pre-existing dead import unrelated to Settings shape: `from app
    import alert_service` (openai_client.py:28, `app/alert_service.py` never ported — see
    ISSUE-010, `app.config` was only half the gap it names). Fixing that is a new module,
    outside this phase's ALLOWED_FILES (`app/config.py`, `tests/test_config.py`) — flagged at
    ANCHOR for Wyatt, not silently pulled into P0 scope. `test_openai_client_import_blocked_by_
    unported_alert_service` below documents the current, still-broken state explicitly instead
    of pretending this test covers it."""
    from app.config import get_settings

    settings = get_settings()
    assert isinstance(settings.openai_model, str) and settings.openai_model
    assert hasattr(settings.reasoning_models, "__contains__")  # supports `model in ...`


def test_openai_client_imports_without_alert_service() -> None:
    """ĐẢO CHIỀU ở spec 07 G0. Trước đó test này là `xfail(strict=True)` ghi nhận sự thật
    ngược lại: `from app import alert_service` ở module level làm cả `openai_client` không
    import nổi (ISSUE-010, `app/alert_service.py` chưa từng được port).

    G0 đổi coupling đó thành hook tiêm vào (`on_rate_limit`), nên import giờ phải SẠCH. Giữ
    test thay vì xoá: nó là canh gác chống ai đó vô tình thêm lại một import module-level vào
    thứ chưa tồn tại.

    ISSUE-010 **vẫn OPEN** — G0 gỡ coupling, KHÔNG port `alert_service`. Hôm nay 429 không
    được đếm ở đâu cả trừ khi caller tiêm hook.
    """
    from agent.providers.openai_client import OpenAIClient  # noqa: F401


@pytest.mark.parametrize(
    ("secret_value", "env", "expect_raise"),
    [
        (None, "production", True),  # chưa set
        ("", "production", True),  # set nhưng rỗng
        ("   ", "production", True),  # chỉ khoảng trắng
        ("a-real-secret-value-32bytes-long!", "production", False),
        (None, "dev", False),  # dev fallback vẫn được phép
    ],
)
def test_blank_env_validator_does_not_loosen_jwt_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    secret_value: str | None,
    env: str,
    expect_raise: bool,
) -> None:
    """`_blank_env_means_unset` (app/config.py) KHÔNG được nới lỏng fail-closed của JWT secret.

    Validator đó xoá mọi biến env có giá trị rỗng/khoảng trắng để default được dùng — vá cho
    bug `TOGETHER_MODEL=` rỗng (spec 07 G1). Nhưng nó áp cho MỌI field, kể cả
    `ohana_jwt_secret`, và `get_jwt_secret()` là path bảo mật: rò secret ⇒ kẻ tấn công forge
    được cookie với `shop_id` bất kỳ ⇒ cross-tenant (R1.22).

    Ban đầu tôi kiểm tay rồi ghi kết quả vào comment trong `app/config.py`. Reviewer từ chối
    coi đó là bằng chứng — đúng: một lần chạy tay không chặn được ai đó đổi validator ngày mai.
    Đây là cùng nguyên tắc "docstring không phải bằng chứng" mà tôi đang áp cho người khác.

    Hàng `("   ", production, raise)` là một thay đổi hành vi CÓ CHỦ Ý so với trước validator:
    secret toàn khoảng trắng từng là truthy nên được dùng làm secret THẬT. Giờ nó bị từ chối.
    Không ai cố tình đặt vậy — nhưng copy/paste hỏng thì có.
    """
    monkeypatch.delenv("OHANA_JWT_SECRET", raising=False)
    if secret_value is not None:
        monkeypatch.setenv("OHANA_JWT_SECRET", secret_value)
    monkeypatch.setenv("OHANA_ENV", env)

    from auth.identity import get_jwt_secret

    if expect_raise:
        with pytest.raises(RuntimeError):
            get_jwt_secret()
    else:
        assert get_jwt_secret(), "phải trả về secret dùng được"


def test_reasoning_models_supports_membership_test() -> None:
    """`openai_client.py:252` does `model in get_settings().reasoning_models` — field must be a
    collection (not e.g. a comma-string) for that to behave as intended."""
    from app.config import get_settings

    assert "gpt-4o" not in get_settings().reasoning_models  # empty by default (P0 doesn't wire)


def test_blank_reasoning_models_env_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISSUE-018: `REASONING_MODELS=` rỗng phải ⇒ default `frozenset()`, KHÔNG raise.

    `_blank_env_means_unset` hứa "rỗng = chưa set" nhưng chỉ phủ field VÔ HƯỚNG: nó chạy
    `mode="before"` = *sau* khi `EnvSettingsSource` đã thử JSON-parse. Với field PHỨC
    (`frozenset[str]`), chuỗi rỗng nổ `SettingsError` ngay tại tầng source, validator không
    bao giờ thấy. Trước fix, dòng này RAISE — cùng lời hứa, thủng đúng ở field phức.
    """
    from app.config import Settings

    monkeypatch.setenv("REASONING_MODELS", "")
    assert Settings().reasoning_models == frozenset()


def test_reasoning_models_env_parsed_as_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Giá trị không rỗng vẫn nạp được thành collection cho membership test."""
    from app.config import Settings

    monkeypatch.setenv("REASONING_MODELS", "gpt-5, o3 ,gpt-4o")
    assert Settings().reasoning_models == frozenset({"gpt-5", "o3", "gpt-4o"})
