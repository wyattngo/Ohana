"""F2 API Q&A gate — bridge/ohana_client.py + tools/ohana_read.py (spec 01 Phase 4 step 12).

Written BEFORE bridge/ohana_client.py and tools/ohana_read.py — expected RED until step 13
lands. PRE-002 (real Ohana platform API endpoint list) is unresolved: this gate is
contract-shape only via httpx.MockTransport. Once PRE-002 clears, a follow-up will layer a
narrow set of live-signature tests on top; the contract locked here does not change.

Contract:
  1. `bridge.ohana_client.OhanaClient(client=None, *, base_url, service_key)` — inject an
     httpx.AsyncClient for tests. `verify=True` is hardcoded on the lazy-built path (R1.3).
  2. `client.call(method, user_id, shop_id, params)` — POST {base_url}/{method}
     with header `X-Ohana-Key: <service_key>` and JSON body carrying BOTH `user_id` and
     `shop_id` written LAST. R1.1 extended: a params dict that smuggles user_id/shop_id
     cannot override the verified values.
  3. Envelope: HTTP 200 + `{"status": true, "data": …}` → returns `data`; `{"status": false}`
     → OhanaAppError; 401 → OhanaAuthError; 429 → OhanaRateLimitError; other >=400 →
     OhanaTransportError; malformed JSON / missing status → OhanaProtocolError.
  4. `tools.ohana_read.build_order_status_tool(client)` → Tool.
     Handler shape: `async (user_id, shop_id, args) → {"success": bool, "data"|"error": …}`.
     Never raises — bridge errors are translated to `{"success": False, "error": <code>}`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bridge.ohana_client import OhanaClient

_BASE = "http://ohana-platform.test"
_KEY = "OHANA_TEST_SVC_KEY"


def _client(handler: Any) -> OhanaClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=_BASE)
    return OhanaClient(http, base_url=_BASE, service_key=_KEY)


# ---- OhanaClient.call — request/response shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_call_happy_path_returns_data_and_request_shape() -> None:
    from bridge.ohana_client import OhanaClient  # noqa: F401 — import surface check

    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["key"] = req.headers.get("X-Ohana-Key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"status": True, "data": {"order_id": "o1", "state": "shipped"}}
        )

    client = _client(handler)
    data = await client.call(
        "order_status", user_id="u1", shop_id="shop_a", params={"order_id": "o1"}
    )

    assert data == {"order_id": "o1", "state": "shipped"}
    assert seen["method"] == "POST"
    assert seen["path"] == "/order_status"
    assert seen["key"] == _KEY
    # R1.1 extended — verified identity fields land in body, params still present.
    assert seen["body"]["user_id"] == "u1"
    assert seen["body"]["shop_id"] == "shop_a"
    assert seen["body"]["order_id"] == "o1"


@pytest.mark.asyncio
async def test_call_verified_ids_override_smuggled_params() -> None:
    """Adversarial: LLM (or a compromised caller) passes user_id/shop_id in params. The
    verified args must overwrite them — the platform must see the JWT-derived identity."""
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": True, "data": None})

    client = _client(handler)
    await client.call(
        "order_status",
        user_id="verified_u",
        shop_id="verified_shop",
        params={"user_id": "attacker", "shop_id": "victim_shop", "order_id": "o1"},
    )

    assert seen["body"]["user_id"] == "verified_u", "smuggled user_id must NOT win"
    assert seen["body"]["shop_id"] == "verified_shop", "smuggled shop_id must NOT win"
    assert seen["body"]["order_id"] == "o1"


@pytest.mark.asyncio
async def test_call_status_false_raises_app_error() -> None:
    from bridge.ohana_client import OhanaAppError

    client = _client(
        lambda req: httpx.Response(200, json={"status": False, "message": "order not found"})
    )
    with pytest.raises(OhanaAppError, match="order not found"):
        await client.call("order_status", user_id="u1", shop_id="s1", params={"order_id": "x"})


@pytest.mark.asyncio
async def test_call_http_401_raises_auth_error() -> None:
    from bridge.ohana_client import OhanaAuthError

    client = _client(lambda req: httpx.Response(401, text="nope"))
    with pytest.raises(OhanaAuthError):
        await client.call("order_status", user_id="u1", shop_id="s1", params={})


@pytest.mark.asyncio
async def test_call_http_429_raises_rate_limit_error() -> None:
    from bridge.ohana_client import OhanaRateLimitError

    client = _client(lambda req: httpx.Response(429, text="slow down"))
    with pytest.raises(OhanaRateLimitError):
        await client.call("order_status", user_id="u1", shop_id="s1", params={})


@pytest.mark.asyncio
async def test_call_bad_json_raises_protocol_error() -> None:
    from bridge.ohana_client import OhanaProtocolError

    client = _client(lambda req: httpx.Response(200, content=b"not-json"))
    with pytest.raises(OhanaProtocolError):
        await client.call("order_status", user_id="u1", shop_id="s1", params={})


@pytest.mark.asyncio
async def test_call_rejects_bad_method_name() -> None:
    """Path-safety guard: method name is a code literal in tools/*, but defence-in-depth against
    injection. `/`, `.`, uppercase → ValueError before any HTTP happens."""
    client = _client(lambda req: httpx.Response(200, json={"status": True, "data": None}))
    for bad in ["../evil", "order/status", "order.status", "Order_Status"]:
        with pytest.raises(ValueError):
            await client.call(bad, user_id="u1", shop_id="s1", params={})


# ---- tools.ohana_read.order_status — handler shape -----------------------------------------------


@pytest.mark.asyncio
async def test_order_status_tool_success_shape() -> None:
    from tools.ohana_read import build_order_status_tool

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": True, "data": {"order_id": "o42", "state": "delivered"}}
        )

    client = _client(handler)
    tool = build_order_status_tool(client)

    result = await tool.handler("u1", "shop_a", {"order_id": "o42"})

    assert result == {"success": True, "data": {"order_id": "o42", "state": "delivered"}}
    assert tool.kind == "read"
    assert tool.name == "order_status"


@pytest.mark.asyncio
async def test_order_status_tool_app_error_becomes_failure_envelope() -> None:
    """Bridge OhanaAppError → handler returns {success: False, error: <code>} — no exception
    reaches the orchestrator."""
    from tools.ohana_read import build_order_status_tool

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": False, "message": "order not found"})

    client = _client(handler)
    tool = build_order_status_tool(client)

    result = await tool.handler("u1", "shop_a", {"order_id": "missing"})

    assert result["success"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_order_status_tool_rejects_missing_order_id() -> None:
    """Handler input validation — no order_id in args → structured failure, no HTTP call made."""
    from tools.ohana_read import build_order_status_tool

    called = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return httpx.Response(200, json={"status": True, "data": None})

    client = _client(handler)
    tool = build_order_status_tool(client)

    result = await tool.handler("u1", "shop_a", {})

    assert result["success"] is False
    assert called["count"] == 0, "handler must reject invalid args before hitting the bridge"
