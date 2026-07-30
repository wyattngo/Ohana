"""ISSUE-010 — bộ đếm provider-429 (`app/alert_service.py`).

Chứng minh: (1) đếm thật, (2) fail-open không raise, (3) chữ ký khớp hook `on_rate_limit` của
client. KHÔNG test đường wire-in ở `api/chat.py` — đó là RISK_PATH, thuộc một ADP phase riêng.
"""

from __future__ import annotations

import inspect

import pytest

from app import alert_service


@pytest.fixture(autouse=True)
def _reset() -> None:
    alert_service._reset_for_test()


@pytest.mark.asyncio
async def test_records_each_429() -> None:
    assert alert_service.provider_429_count() == 0
    await alert_service.record_provider_429()
    await alert_service.record_provider_429()
    await alert_service.record_provider_429()
    assert alert_service.provider_429_count() == 3


@pytest.mark.asyncio
async def test_record_never_raises_even_if_logging_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Được gọi từ khối except của client — một raise ở đây sẽ che mất `RateLimitError`."""

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("logging sink down")

    monkeypatch.setattr(alert_service.logger, "warning", _boom)
    # Không được ném; bộ đếm vẫn tiến (increment xảy ra trước khi log).
    await alert_service.record_provider_429()
    assert alert_service.provider_429_count() == 1


def test_signature_matches_on_rate_limit_hook() -> None:
    """`record_provider_429` phải là `() -> Awaitable[None]` để tiêm thẳng vào client."""
    sig = inspect.signature(alert_service.record_provider_429)
    assert list(sig.parameters) == []
    assert inspect.iscoroutinefunction(alert_service.record_provider_429)


def test_client_still_imports_without_module_level_alert_service() -> None:
    """Canh gác ISSUE-010: tạo module KHÔNG được kéo lại import module-level vào client.

    Trùng chủ đích với `test_config.test_openai_client_imports_without_alert_service`; đặt cạnh
    module để ai sửa `alert_service` thấy ràng buộc ngay tại đây.
    """
    import agent.providers.openai_client as oc

    src = inspect.getsource(oc)
    assert "from app import alert_service" not in src
    assert "import app.alert_service" not in src
