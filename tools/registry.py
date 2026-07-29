"""Tool dataclass — the shape every agent tool must fit.

Ported from drnickv4 with the R1.1 signature widened for multi-tenant: a handler receives
`(user_id, shop_id, args)` — `user_id` AND `shop_id` are separate args the orchestrator
supplies from a verified `auth.identity.Identity`; neither can appear in `parameters`, so
the LLM cannot direct a tool at another user or another shop.

Spec 15 P1 retired `register()` + `TOOLS` global: the drafter takes tools via
constructor DI (spec 13 `LLMDrafter.__init__(tools=...)`), so an app-startup
mutable global was dead. `Tool` stays — DI still uses this shape.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

ToolHandler = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema — feeds the LLM tools= param AND input validation
    handler: ToolHandler  # async (user_id, shop_id, args) -> {"success": bool, ...}
    kind: Literal["read", "action"] = "read"  # READ runs directly; ACTION → policy_gate (Phase 5)
