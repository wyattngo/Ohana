"""Provider-429 telemetry sink (ISSUE-010, nửa `alert_service` còn OPEN).

Bối cảnh. `agent/providers/openai_client.py` gọi một hook `on_rate_limit` mỗi khi provider trả
HTTP 429, rồi RE-RAISE `RateLimitError` nguyên vẹn (fire-and-forget). Hook mặc định là `None` ⇒
hôm nay **429 không được đếm ở đâu cả**. Module này cung cấp một đích tiêm cho hook đó.

Phạm vi CÓ CHỦ Ý hẹp. Bản `app/alert_service.py` bên `drnickv4` là module đa-spec (34/36/40):
webhook sink + cooldown Redis + bộ đếm nhiều nguồn + poller lifespan, phụ thuộc `health_service`
và `latency_service` — cả hai CHƯA port sang ohana, và Redis cũng CHƯA wire. Port nguyên = kéo
theo ba spec chưa thuộc scope. Ở đây chỉ có ĐÚNG bộ đếm 429, đủ để đóng khoảng trống capability
mà ISSUE-010 nêu, không hơn.

MVP in-process. Bộ đếm nằm trong bộ nhớ tiến trình, KHÔNG Redis — trung thực cho MVP một tiến
trình. Khi Redis được wire, thay ruột `record_provider_429`/`provider_429_count` bằng
`INCRBY`+`EXPIRE` (đúng hình dạng drnick) mà KHÔNG đổi chữ ký — call-site không phải sửa lại.

Fail-OPEN tuyệt đối. `record_provider_429` được gọi TỪ TRONG khối `except RateLimitError` của
client; một exception thoát ra đây sẽ thay `RateLimitError` bằng lỗi khác và nuốt mất tín hiệu
429. Vì vậy nó không bao giờ raise, kể cả khi logging hỏng.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Bộ đếm process-local. Lock vì `record` có thể bị gọi từ nhiều task/thread song song (mỗi
# 429 trên các request khác nhau). `int += 1` không nguyên tử dưới free-threading tương lai.
_lock = threading.Lock()
_provider_429_total = 0


async def record_provider_429() -> None:
    """Đếm một lần provider trả 429 (chữ ký khớp hook `on_rate_limit: () -> Awaitable[None]`).

    Fire-and-forget, KHÔNG raise: được gọi từ khối except của client, một lỗi ở đây sẽ che mất
    chính `RateLimitError` mà client cần re-raise. Mỗi 429 cũng ghi một dòng WARN có cấu trúc,
    nên tín hiệu quan sát được ngay cả trước khi có reader tổng hợp.
    """
    global _provider_429_total
    # Increment là thuần bộ nhớ, không thể raise ⇒ để NGOÀI try, tách khỏi số phận của logging.
    with _lock:
        _provider_429_total += 1
        total = _provider_429_total
    # Chỉ logging mới có thể hỏng (sink chết). Nuốt thật — KHÔNG gọi lại logger trong except,
    # đó chính là bẫy tự-che-lỗi mà test `..._even_if_logging_breaks` bắt được.
    try:
        logger.warning("provider_429 total=%d", total)
    except Exception:  # noqa: S110  # pragma: no cover
        # Cố ý nuốt: thứ đang hỏng CHÍNH LÀ logging, nên "log lỗi thay vì pass" (điều S110 khuyên)
        # là bất khả — và một raise ở đây sẽ che mất `RateLimitError` client cần re-raise.
        pass


def provider_429_count() -> int:
    """Tổng số 429 đã đếm trong đời tiến trình này (reader cho quan sát/test)."""
    with _lock:
        return _provider_429_total


def _reset_for_test() -> None:
    """Đưa bộ đếm về 0. CHỈ dùng trong test để cô lập giữa các ca — không gọi ở đường chạy thật."""
    global _provider_429_total
    with _lock:
        _provider_429_total = 0
