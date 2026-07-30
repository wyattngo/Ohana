"""D1 gate — `LLMDrafter` tool-loop grounding (spec 13 Phase D1).

RED-first (RISK:high): dựng `LLMDrafter(llm, sf, tools=[...])` — chữ ký D0 chưa nhận `tools`,
nên các test này đỏ tới khi loop grounding land.

Vì sao các test này:
  - `shop_id` xuống handler PHẢI từ tham số `draft()` (verified), KHÔNG từ tool args của LLM —
    nếu không, model nhắc tên shop khác trong câu hỏi có thể tra dữ liệu shop khác (R1.1,
    roadmap §8 "Multi-tenant data leak" HIGH).
  - tool result PHẢI xâu vào lượt LLM sau — nếu không, grounding vô nghĩa, model vẫn đoán.
  - cap vòng lặp: model gọi tool mãi KHÔNG được treo tiến trình; raise dứt khoát.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from agent.drafter import LLMDrafter
from agent.llm_client import AssistantStep, ToolCall
from tools.registry import Tool


class _RecordingLLM:
    """Fake LLM: replays preset steps theo thứ tự, ghi lại messages MỖI lượt (cho assert loop)."""

    def __init__(self, steps: list[AssistantStep]) -> None:
        self._steps = list(steps)
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.seen_tools: list[list[dict[str, Any]] | None] = []

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


def _content_step(text: str) -> AssistantStep:
    """Model trả lời bằng content, KHÔNG gọi tool nào (hành vi thật sau grounding)."""
    return AssistantStep(content=text, tool_calls=[], usage=None)


def _tool_step(name: str, args: dict[str, Any], *, call_id: str = "tc1") -> AssistantStep:
    return AssistantStep(
        content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=args)], usage=None
    )


def _emit_step(*, text: str, intent: str, confidence: float) -> AssistantStep:
    return AssistantStep(
        content=None,
        tool_calls=[
            ToolCall(
                id="emit-1",
                name="emit_reply",
                arguments={"text": text, "intent": intent, "confidence": confidence},
            )
        ],
        usage=None,
    )


def _recording_tool(name: str) -> tuple[Tool, dict[str, Any]]:
    """`Tool` mà handler GHI LẠI `(user_id, shop_id, args)` nó nhận, để test soi."""
    seen: dict[str, Any] = {}

    async def handler(user_id: str, shop_id: str, args: dict[str, Any]) -> dict[str, Any]:
        seen["user_id"] = user_id
        seen["shop_id"] = shop_id
        seen["args"] = dict(args)
        return {"success": True, "result": "M"}

    tool = Tool(
        name=name,
        description="test lookup",
        parameters={
            "type": "object",
            "properties": {"height_cm": {"type": "integer"}, "weight_kg": {"type": "integer"}},
            "required": ["height_cm", "weight_kg"],
            "additionalProperties": False,
        },
        handler=handler,
        kind="read",
    )
    return tool, seen


async def _seed_shop(engine: Any, sf: Any) -> str:
    shop = f"shop_{uuid.uuid4().hex[:12]}"
    async with engine.begin() as c:
        await c.execute(
            sa.text("insert into shops (id, name, status) values (:i, :n, 'active')"),
            {"i": shop, "n": f"Shop {shop}"},
        )
    return shop


@pytest.mark.asyncio
async def test_grounding_tool_dispatched_with_real_shop_id_not_llm_args(fresh_db) -> None:
    """(a) `shop_id` handler nhận = param `draft()`, KHÔNG phải giá trị giả LLM nhét vào args."""
    engine, sf = await fresh_db()
    shop = await _seed_shop(engine, sf)
    tool, seen = _recording_tool("lookup_size")

    llm = _RecordingLLM(
        [
            # Lượt 1: model gọi lookup_size — CỐ Ý nhét shop_id giả vào args.
            _tool_step("lookup_size", {"height_cm": 160, "weight_kg": 50, "shop_id": "BOGUS-SHOP"}),
            # Lượt 2: model kết bằng emit_reply.
            _emit_step(text="Bạn mặc size M nhé", intent="general", confidence=0.9),
        ]
    )
    r = await LLMDrafter(llm, sf, tools=[tool]).draft(
        shop_id=shop, customer_id="cust1", message="1m6 50kg mặc gì", history=[]
    )

    assert seen["shop_id"] == shop, "handler nhận shop_id sai — không phải từ param draft()"
    assert seen["shop_id"] != "BOGUS-SHOP", "shop_id giả trong LLM args LỌT xuống handler"
    assert seen["user_id"] == "cust1", "user_id phải là customer_id"
    assert (r.text, r.intent, r.confidence) == ("Bạn mặc size M nhé", "general", 0.9)


@pytest.mark.asyncio
async def test_tool_result_threaded_into_next_llm_turn(fresh_db) -> None:
    """(b) Kết quả tool xâu vào lượt LLM SAU (role=tool), kèm assistant tool_call turn."""
    engine, sf = await fresh_db()
    shop = await _seed_shop(engine, sf)
    tool, _ = _recording_tool("lookup_size")

    llm = _RecordingLLM(
        [
            _tool_step("lookup_size", {"height_cm": 160, "weight_kg": 50}),
            _emit_step(text="Size M", intent="general", confidence=0.9),
        ]
    )
    await LLMDrafter(llm, sf, tools=[tool]).draft(
        shop_id=shop, customer_id="cust1", message="1m6 50kg", history=[]
    )

    assert len(llm.seen_messages) == 2, "phải có đúng 2 lượt LLM (tool rồi emit)"
    round2 = llm.seen_messages[1]
    tool_msgs = [m for m in round2 if m.get("role") == "tool"]
    assert tool_msgs, "kết quả tool không được xâu vào lượt sau"
    assert any("M" in str(m.get("content", "")) for m in tool_msgs), "tool result thiếu nội dung"
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in round2), (
        "thiếu assistant tool_call turn — provider correlation sẽ hỏng"
    )


@pytest.mark.asyncio
async def test_loop_cap_raises_not_hangs(fresh_db) -> None:
    """(c) Model gọi tool vô hạn ⇒ raise sau cap, KHÔNG treo, KHÔNG trả draft bịa."""
    engine, sf = await fresh_db()
    shop = await _seed_shop(engine, sf)
    tool, _ = _recording_tool("lookup_size")

    # 20 lượt đều là tool_call, không bao giờ emit_reply.
    steps = [
        _tool_step("lookup_size", {"height_cm": 1, "weight_kg": 1}, call_id=f"tc{i}")
        for i in range(20)
    ]
    with pytest.raises(ValueError):
        await LLMDrafter(_RecordingLLM(steps), sf, tools=[tool]).draft(
            shop_id=shop, customer_id="cust1", message="x", history=[]
        )


@pytest.mark.asyncio
async def test_content_answer_after_grounding_triggers_forced_emit(fresh_db) -> None:
    """(d) Model tra tool xong trả lời bằng content (bỏ emit_reply) ⇒ 1 lượt cuối ép emit_reply.

    Đây là hành vi THẬT của Llama-3.3 mà smoke D1 bắt được (RETRY 1, hướng 1). Fallback CHỈ khi
    có grounding tool; lượt ép phải offer DUY NHẤT emit_reply (D0: model gọi tin cậy khi đó là
    tool duy nhất). intent/confidence vẫn từ LLM, KHÔNG bịa.
    """
    engine, sf = await fresh_db()
    shop = await _seed_shop(engine, sf)
    tool, _ = _recording_tool("lookup_size")

    llm = _RecordingLLM(
        [
            _tool_step("lookup_size", {"height_cm": 160, "weight_kg": 50}),  # lượt 1: grounding
            _content_step("Bạn mặc size XL3 nhé"),  # lượt 2: trả content, KHÔNG emit_reply
            _emit_step(text="Bạn mặc size XL3 nhé", intent="general", confidence=0.9),  # lượt ép
        ]
    )
    r = await LLMDrafter(llm, sf, tools=[tool]).draft(
        shop_id=shop, customer_id="cust1", message="1m6 50kg", history=[]
    )

    assert (r.text, r.intent, r.confidence) == ("Bạn mặc size XL3 nhé", "general", 0.9)
    assert len(llm.seen_tools) == 3, "phải có lượt ép emit_reply thứ 3"
    # Lượt ép CHỈ offer emit_reply — không kèm grounding tool.
    final_names = [t["name"] for t in (llm.seen_tools[2] or [])]
    assert final_names == ["emit_reply"], f"lượt ép phải chỉ có emit_reply, có: {final_names}"


@pytest.mark.asyncio
async def test_no_tools_still_works_single_emit(fresh_db) -> None:
    """(d) Backward-compat: không tools (D0 shape) ⇒ model emit_reply lượt đầu, không lặp."""
    engine, sf = await fresh_db()
    shop = await _seed_shop(engine, sf)

    llm = _RecordingLLM([_emit_step(text="Dạ còn ạ", intent="general", confidence=0.88)])
    r = await LLMDrafter(llm, sf).draft(
        shop_id=shop, customer_id="cust1", message="còn hàng ko", history=[]
    )
    assert (r.text, r.intent, r.confidence) == ("Dạ còn ạ", "general", 0.88)
    assert len(llm.seen_messages) == 1
