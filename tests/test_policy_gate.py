"""Policy-gate decision logic (spec 01 Phase 5 step 14).

Pure function tests — no DB, no HTTP. The gate encodes ONE rule that MUST hold regardless
of confidence, `auto_enabled`, or shop settings: sensitive intents (complaint / refund /
price_negotiation / specific_order) ALWAYS park. This is the acceptance-blocking safety
net from spec §4 and §12 — there is no code path that lets an auto-send bypass it.

Written BEFORE `agent/policy_gate.py`; expected RED until step 15 lands.
"""

from __future__ import annotations

import pytest


def test_sensitive_intent_always_parks_even_with_high_confidence_and_auto_enabled() -> None:
    """The blocklist wins over confidence AND shop settings. If this ever regresses, an angry
    customer's refund request could be auto-sent a wrong reply — this is the acceptance-block
    rule the whole gate exists to protect."""
    from agent.policy_gate import DraftContext, decide

    for intent in ("complaint", "refund", "price_negotiation", "specific_order"):
        d = decide(
            DraftContext(
                confidence=0.99,
                intent=intent,
                shop_auto_enabled_for_intent=True,
            )
        )
        assert d.action == "park", f"{intent} must park but got {d.action}"
        assert "sensitive" in d.reason, f"expected sensitive_intent reason, got {d.reason!r}"


def test_low_confidence_parks_even_for_safe_intent() -> None:
    """Confidence below threshold → park regardless of intent / auto_enabled. Prevents shipping
    a wrong-answer that survived intent-safety just because the shop opted into auto-send."""
    from agent.policy_gate import DraftContext, decide

    d = decide(
        DraftContext(
            confidence=0.5,
            intent="general_qa",
            shop_auto_enabled_for_intent=True,
        )
    )
    assert d.action == "park"
    assert "confidence" in d.reason.lower() or "low" in d.reason.lower()


def test_auto_disabled_for_intent_parks_even_at_high_confidence() -> None:
    """A shop that hasn't opted into auto-send for this intent kind → always park, even if we
    are confident and the intent is otherwise safe."""
    from agent.policy_gate import DraftContext, decide

    d = decide(
        DraftContext(
            confidence=0.99,
            intent="general_qa",
            shop_auto_enabled_for_intent=False,
        )
    )
    assert d.action == "park"


def test_high_confidence_safe_intent_auto_enabled_sends() -> None:
    """All three conditions met → auto_send. The ONE path that lets a draft reach a customer
    without a seller in the loop."""
    from agent.policy_gate import DraftContext, decide

    d = decide(
        DraftContext(
            confidence=0.95,
            intent="general_qa",
            shop_auto_enabled_for_intent=True,
        )
    )
    assert d.action == "auto_send"


def test_default_threshold_conservative() -> None:
    """The DEFAULT threshold must NOT be permissive by accident (a config drift catch — if
    someone bumps it down to 0.5 the tests here would still pass, this test asserts the value
    stays high enough to keep auto-send rare in GĐ0)."""
    from agent.policy_gate import DEFAULT_CONFIDENCE_THRESHOLD

    assert DEFAULT_CONFIDENCE_THRESHOLD >= 0.8


def test_sensitive_intents_frozen() -> None:
    """The blocklist is a frozenset — attempts to mutate it at runtime raise. Prevents a
    later refactor from swapping in a Python set that a stray line could `.discard()`."""
    from agent.policy_gate import SENSITIVE_INTENTS

    assert isinstance(SENSITIVE_INTENTS, frozenset)
    assert {"complaint", "refund", "price_negotiation", "specific_order"} <= SENSITIVE_INTENTS
    with pytest.raises(AttributeError):
        SENSITIVE_INTENTS.add("safe_intent")  # type: ignore[attr-defined]
