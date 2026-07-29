"""Draft-to-customer policy gate (spec 01 §3 Sub-task E, §4 acceptance-blocking).

The ONE code path that lets a draft reach a customer without a seller in the loop. Every
decision surface flows through `decide(...)`; there is no back-door for auto-send.

Rule precedence (highest wins):

  1. Sensitive intent (complaint, refund, price_negotiation, specific_order) → PARK.
     Applies even when confidence is 1.0 and the shop has opted into auto-send. Spec §4
     "user trust" flag: these categories BURN the seller if AI gets them wrong.
  2. Confidence below `threshold` → PARK. Prevents a low-quality draft from riding the
     safe-intent lane just because the shop opted in.
  3. Shop not opted into auto-send for this intent → PARK. Shop-level consent.
  4. Otherwise → AUTO_SEND.

The blocklist is a `frozenset` — a stray `.discard()` in a later refactor would raise at
runtime rather than silently poke a hole through the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Conservative default (spec §4 "gate là điều kiện ship, không optional"). Bumping this
# DOWN is an F3-scope change and must land as its own spec revision, not a config toggle.
DEFAULT_CONFIDENCE_THRESHOLD = 0.85

SENSITIVE_INTENTS: frozenset[str] = frozenset(
    {"complaint", "refund", "price_negotiation", "specific_order"}
)


@dataclass(frozen=True)
class DraftContext:
    confidence: float  # 0..1, the drafter's own self-reported confidence
    intent: str  # code — matched against SENSITIVE_INTENTS
    shop_auto_enabled_for_intent: bool  # shop-level opt-in for THIS intent


@dataclass(frozen=True)
class DraftDecision:
    action: Literal["auto_send", "park"]
    # stable code: park:sensitive_intent | park:low_confidence
    #            | park:auto_disabled_for_intent | send:within_policy
    reason: str


def decide(ctx: DraftContext, *, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> DraftDecision:
    if ctx.intent in SENSITIVE_INTENTS:
        return DraftDecision(action="park", reason="park:sensitive_intent")
    if ctx.confidence < threshold:
        return DraftDecision(action="park", reason="park:low_confidence")
    if not ctx.shop_auto_enabled_for_intent:
        return DraftDecision(action="park", reason="park:auto_disabled_for_intent")
    return DraftDecision(action="auto_send", reason="send:within_policy")
