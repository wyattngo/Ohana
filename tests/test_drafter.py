"""D0 gate — `agent.drafter.LLMDrafter` (spec 13 Phase D0).

RED-first (RISK:high): written BEFORE `agent/drafter.py` exists, so the whole module
fails to import until the phase is implemented. Each test pins one clause of the D0 GOAL.

Vì sao các test này, không phải "gọi LLM ra chữ là xong":
  - intent/confidence PHẢI đến từ args `emit_reply` của LLM, KHÔNG hardcode — nếu drafter tự
    đặt confidence, nó lái `policy_gate` auto_send bằng một con số bịa (roadmap §8 HIGH).
  - persona PHẢI vào system prompt — nếu không, "AI Seller nói giọng shop" chỉ là lời hứa.
  - ranh giới import: drafter CHỈ sinh draft; quyết gửi/park là của orchestrator. Ràng buộc
    này phải là thuộc tính CẤU TRÚC (không có đường import tới sender), không phải kỷ luật.
Behavior model-thật (no-identity-leak) đo ở `tests/test_drafter_live.py -m live`, không ở đây:
  fake LLM chứng minh drafter RÁP prompt đúng, KHÔNG chứng minh model THẬT tuân chỉ dẫn.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from agent.drafter import EMIT_REPLY_TOOL, INTENT_CODES, LLMDrafter
from agent.llm_client import AssistantStep, ToolCall
from agent.persona import build_persona_prompt
from agent.policy_gate import SENSITIVE_INTENTS
from db.models import Message
from db.repos import ShopProfileRepo

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ── fakes / helpers ──────────────────────────────────────────────────────────


class _RecordingLLM:
    """Fake `LLMClient` — records the messages+tools it was handed, replays preset steps.

    Duck-typed (không subclass ABC): `step()` là thứ drafter gọi; `stream`/`complete` không
    cần cho D0. Cùng khuôn `_FakeLLM` trong test_chat_endpoint.py.
    """

    def __init__(self, steps: list[AssistantStep]) -> None:
        self._steps = list(steps)
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.seen_tools: list[list[dict[str, Any]] | None] = []
        self._default_model = "fake-model"

    async def step(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AssistantStep:
        self.seen_messages.append(list(messages))
        self.seen_tools.append(tools)
        return self._steps.pop(0)


def _emit_step(*, text: str, intent: str, confidence: float) -> AssistantStep:
    """An AssistantStep whose sole tool_call is `emit_reply(text, intent, confidence)`."""
    return AssistantStep(
        content=None,
        tool_calls=[
            ToolCall(
                id="emit-1",
                name="emit_reply",
                arguments={"text": text, "intent": intent, "confidence": confidence},
            )
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


async def _seed_shop_with_persona(engine: Any, sf: Any, *, persona: str) -> str:
    shop = _uid("shop")
    async with engine.begin() as c:
        await c.execute(
            sa.text("insert into shops (id, name, status) values (:i, :n, 'active')"),
            {"i": shop, "n": f"Shop {shop}"},
        )
    async with sf() as s:
        await ShopProfileRepo(s, shop_scope=shop).upsert(persona_md=persona, knowledge={})
        await s.commit()
    return shop


# ── tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_intent_and_confidence_come_from_llm_not_hardcoded(fresh_db) -> None:
    """(a) Clause chính: `intent`+`confidence` là args `emit_reply` của LLM, KHÔNG hằng số.

    Chạy drafter với HAI payload khác nhau ⇒ phải surface đúng cả hai. Một drafter hardcode
    confidence sẽ trả cùng một số cho cả hai và test này đỏ.
    """
    engine, sf = await fresh_db()
    shop = await _seed_shop_with_persona(engine, sf, persona="Giọng thân thiện")

    llm_a = _RecordingLLM([_emit_step(text="Dạ còn ạ", intent="general", confidence=0.92)])
    r_a = await LLMDrafter(llm_a, sf).draft(
        shop_id=shop, customer_id="cust1", message="còn hàng ko", history=[]
    )
    assert (r_a.text, r_a.intent, r_a.confidence) == ("Dạ còn ạ", "general", 0.92)

    llm_b = _RecordingLLM([_emit_step(text="Cho em xin lỗi ạ", intent="complaint", confidence=0.4)])
    r_b = await LLMDrafter(llm_b, sf).draft(
        shop_id=shop, customer_id="cust1", message="hàng lỗi", history=[]
    )
    assert (r_b.text, r_b.intent, r_b.confidence) == ("Cho em xin lỗi ạ", "complaint", 0.4)


@pytest.mark.asyncio
async def test_persona_prompt_injected_into_system_message(fresh_db) -> None:
    """(b) `build_persona_prompt(...)` phải nằm trong system message gửi tới LLM."""
    engine, sf = await fresh_db()
    persona = "Shop thời trang nữ, xưng em, thân thiện"
    shop = await _seed_shop_with_persona(engine, sf, persona=persona)

    llm = _RecordingLLM([_emit_step(text="ok", intent="general", confidence=0.9)])
    await LLMDrafter(llm, sf).draft(
        shop_id=shop, customer_id="cust1", message="ship q7 nhiêu", history=[]
    )

    systems = [m for m in llm.seen_messages[0] if m["role"] == "system"]
    assert systems, "không có system message nào — persona không được tiêm"
    expected_persona = build_persona_prompt(persona, shop_display_name=f"Shop {shop}")
    # Spec 16 C0: system prompt = persona + _INJECTION_DIRECTIVE (+ _GROUNDING_DIRECTIVE
    # khi có tool). Đổi equality → substring check: persona vẫn có mặt nguyên vẹn, chỉ
    # được append thêm directive. Ép cứng equality = cấm bất kỳ mở rộng nào, vô lý cho
    # spec đang chủ động nhét thêm directive vào cùng chỗ.
    assert any(expected_persona in m["content"] for m in systems), (
        "system message không chứa build_persona_prompt output — persona/display_name sai"
    )


@pytest.mark.asyncio
async def test_history_threaded_between_system_and_current_message(fresh_db) -> None:
    """(c) history xâu ĐÚNG THỨ TỰ, trước `message` hiện tại, sau system."""
    engine, sf = await fresh_db()
    shop = await _seed_shop_with_persona(engine, sf, persona="x")

    history = [
        Message(shop_id=shop, conversation_id="c1", customer_id="cust1", role="user", content="H1"),
        Message(
            shop_id=shop,
            conversation_id="c1",
            customer_id="cust1",
            role="assistant",
            content="A1",
        ),
    ]
    llm = _RecordingLLM([_emit_step(text="ok", intent="general", confidence=0.9)])
    await LLMDrafter(llm, sf).draft(
        shop_id=shop, customer_id="cust1", message="H2", history=history
    )

    contents = [m["content"] for m in llm.seen_messages[0]]

    # H1, A1 xâu nguyên văn (history KHÔNG wrap tag — spec 16 C0: chỉ wrap tin đang soạn,
    # history có role tách bạch rồi). H2 (message hiện tại) được wrap <customer_message>.
    def _find(needle: str) -> int:
        for i, c in enumerate(contents):
            if needle in c:
                return i
        raise ValueError(f"{needle!r} không có trong contents={contents!r}")

    assert _find("H1") < _find("A1") < _find("H2")
    # H2 phải nằm trong tag customer_message (spec 16 C0).
    assert any("<customer_message>H2</customer_message>" in c for c in contents), (
        f"H2 không được wrap <customer_message> — contents={contents!r}"
    )
    # system đứng đầu.
    assert llm.seen_messages[0][0]["role"] == "system"


def test_emit_reply_enum_covers_sensitive_intents() -> None:
    """(d) enum của `emit_reply.intent` PHẢI bao trọn 4 mã nhạy cảm của policy_gate.

    Nếu thiếu một mã, model không có cách phát ra nó ⇒ intent nhạy cảm đó KHÔNG BAO GIỜ park,
    tức lọt gate âm thầm. Đây là ràng buộc liên-module, kiểm bằng tập hợp.
    """
    enum = set(EMIT_REPLY_TOOL["parameters"]["properties"]["intent"]["enum"])
    assert SENSITIVE_INTENTS <= enum, f"thiếu mã nhạy cảm trong enum: {SENSITIVE_INTENTS - enum}"
    assert SENSITIVE_INTENTS <= set(INTENT_CODES)
    assert enum == set(INTENT_CODES), "EMIT_REPLY_TOOL enum và INTENT_CODES phải là một nguồn"


@pytest.mark.asyncio
async def test_missing_emit_reply_call_raises_not_fabricate(fresh_db) -> None:
    """(e) Model trả content thay vì gọi `emit_reply` ⇒ raise, KHÔNG bịa confidence.

    Trả về một draft với confidence mặc định ở đây sẽ là đúng loại hỏng-âm-thầm mà spec này
    tồn tại để chặn: một con số bịa lái auto_send.
    """
    engine, sf = await fresh_db()
    shop = await _seed_shop_with_persona(engine, sf, persona="x")

    llm = _RecordingLLM([AssistantStep(content="tôi nghĩ là còn hàng", tool_calls=[], usage=None)])
    with pytest.raises(ValueError):
        await LLMDrafter(llm, sf).draft(
            shop_id=shop, customer_id="cust1", message="còn ko", history=[]
        )


# ── import-boundary gate ─────────────────────────────────────────────────────


def _first_party_imports(path: Path) -> set[str]:
    roots = {
        "api",
        "app",
        "agent",
        "auth",
        "bridge",
        "channels",
        "db",
        "parsing",
        "retrieval",
        "storage",
        "tools",
    }
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in roots:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in roots:
                found.add(node.module)
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


def _module_path(dotted: str) -> Path | None:
    p = _REPO_ROOT / (dotted.replace(".", "/") + ".py")
    if p.is_file():
        return p
    pkg = _REPO_ROOT / dotted.replace(".", "/") / "__init__.py"
    return pkg if pkg.is_file() else None


def _transitive_first_party(entry: str) -> set[str]:
    seen: set[str] = set()
    queue = [entry]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = _module_path(mod)
        if path is None:
            continue
        for child in _first_party_imports(path):
            if child not in seen:
                queue.append(child)
    return seen


def test_drafter_module_cannot_reach_the_send_path() -> None:
    """Gate ranh giới: drafter CHỈ sinh draft. Không có đường import tới cổng gửi/duyệt.

    Forbidden theo MODULE (không dùng substring "PendingReply"): drafter import
    `ShopProfileRepo` từ `db/repos.py`, mà file đó cũng import model `PendingReply` — nên
    `db.models.PendingReply` NẰM TRONG closure một cách hợp lệ và vô hại (chỉ là định nghĩa
    model, không phải hành động gửi). Thứ thật sự cấm là các module HÀNH ĐỘNG: policy_gate,
    orchestrator, channel adapter, sender.
    """
    reachable = _transitive_first_party("agent.drafter")
    assert _module_path("agent.drafter") is not None, "không tìm thấy agent/drafter.py"
    assert len(reachable) > 1, "agent/drafter.py không import gì — gate có thể vô nghĩa"

    forbidden = {
        "agent.policy_gate": "cổng quyết định gửi/park — drafter không được quyết",
        "agent.orchestrator": "luồng gửi/park khách",
        "bridge.zalo_sender": "sender = đường ra tới khách",
        "channels.zalo": "adapter kênh = đường ra tới khách",
        "channels.base": "abstraction kênh gửi",
    }
    hits = [f"{m} ({why})" for m, why in forbidden.items() if m in reachable]
    assert not hits, "agent/drafter.py với tới đường gửi khách qua: " + "; ".join(hits)
    # Bắt cả sender/channel đặt tên khác trong tương lai: không module gốc bridge/ hay channels/.
    leaked = [m for m in reachable if m.split(".")[0] in {"bridge", "channels"}]
    assert not leaked, f"drafter chạm bridge/channels: {leaked}"
