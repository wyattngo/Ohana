"""Spec 12 W0 — 429 telemetry được wire vào đường chat thật (ISSUE-010).

Hai test, hai tầng:
  (a) đặc-tả chuỗi client↔hook: một `RateLimitError` re-raise nguyên vẹn VÀ bộ đếm tăng. Đây
      là hành vi đã có ở `OpenAIClient._create` + `alert_service` — giữ làm regression anchor.
  (b) pin đúng thay đổi của phase: `get_llm_client()` truyền `record_provider_429` làm hook.
      ĐỎ trước khi wire (client dựng không hook), XANH sau.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from agent.providers.openai_client import OpenAIClient
from app import alert_service


@pytest.fixture(autouse=True)
def _reset() -> None:
    alert_service._reset_for_test()


class _RaisingCompletions:
    async def create(self, **_kwargs: object) -> object:
        raise openai.RateLimitError(
            "rate limited",
            response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            body=None,
        )


class _RaisingClient:
    class chat:  # noqa: N801 - khớp shape `client.chat.completions.create`
        completions = _RaisingCompletions()


@pytest.mark.asyncio
async def test_client_counts_429_and_reraises_intact() -> None:
    client = OpenAIClient(
        client=_RaisingClient(),
        default_model="m",
        api_key="x",
        on_rate_limit=alert_service.record_provider_429,
    )
    with pytest.raises(openai.RateLimitError):
        await client._create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert alert_service.provider_429_count() == 1


def test_get_llm_client_wires_the_429_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED trước wire: `get_llm_client` dựng `TogetherClient()` không hook ⇒ hook is None."""
    import api.chat as chat

    captured: dict[str, object] = {}

    class _Spy:
        def __init__(self, *_a: object, on_rate_limit: object = None, **_k: object) -> None:
            captured["hook"] = on_rate_limit

    monkeypatch.setattr("agent.providers.together_client.TogetherClient", _Spy)
    monkeypatch.setattr(chat, "_client_cache", None)

    chat.get_llm_client()

    assert captured["hook"] is alert_service.record_provider_429
