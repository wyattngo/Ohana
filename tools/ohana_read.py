"""F2 read-tools — call the Ohana platform REST API through `bridge.ohana_client.OhanaClient`.

Each tool returns a `{"success": bool, "data"|"error": …}` envelope — bridge exceptions are
CAUGHT here and translated to structured failures. The orchestrator receives a clean
envelope for every tool call, never a raw exception (which would leak internal state to
the LLM's tool-result view).

R1.1 EXTENDED — every handler receives `(user_id, shop_id, args)`; both identity fields
come from the verified `auth.identity.Identity`, and are forwarded as SEPARATE args to
`OhanaClient.call(..., user_id=..., shop_id=..., params=...)`. `args` is the raw dict the
LLM emitted; it MUST NOT be trusted to carry identity.

PRE-002 unresolved — GĐ0 ships only `order_status`. Additional read-tools (`shipping_info`,
`product_info`, `account_lookup` per spec §3 Sub-task D) land when the platform endpoint
spec is confirmed.
"""

from __future__ import annotations

from typing import Any

from bridge.ohana_client import (
    OhanaAppError,
    OhanaAuthError,
    OhanaClient,
    OhanaError,
    OhanaProtocolError,
    OhanaRateLimitError,
    OhanaTransportError,
)
from tools.registry import Tool

_ORDER_STATUS_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_id": {
            "type": "string",
            "description": "Platform order id to look up.",
        },
    },
    "required": ["order_id"],
    "additionalProperties": False,
}


def _classify_error(exc: OhanaError) -> str:
    """Map bridge exceptions to short error codes for the tool envelope. Keep the codes
    stable — the orchestrator (and any future retry policy) branches on them."""
    if isinstance(exc, OhanaAppError):
        return exc.reason or "app_error"
    if isinstance(exc, OhanaAuthError):
        return "auth_error"
    if isinstance(exc, OhanaRateLimitError):
        return "rate_limit"
    if isinstance(exc, OhanaProtocolError):
        return "protocol_error"
    if isinstance(exc, OhanaTransportError):
        return "transport_error"
    return "unknown_error"


def build_order_status_tool(client: OhanaClient) -> Tool:
    """`order_status(order_id) → {success, data|error}` — look up one order's state."""

    async def handler(user_id: str, shop_id: str, args: dict[str, Any]) -> dict[str, Any]:
        order_id = args.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            return {"success": False, "error": "invalid_order_id"}
        try:
            data = await client.call(
                "order_status",
                user_id=user_id,
                shop_id=shop_id,
                params={"order_id": order_id},
            )
        except OhanaError as exc:
            return {"success": False, "error": _classify_error(exc)}
        return {"success": True, "data": data}

    return Tool(
        name="order_status",
        description="Look up the status of one order by id.",
        parameters=_ORDER_STATUS_PARAMS,
        handler=handler,
        kind="read",
    )
