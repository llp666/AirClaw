"""POST /api/chat —— SSE 流式对话。

系统核心端点。内部流程（README「chat.py — 流式对话」）：

    1. load_for_agent() 取经合并优化的历史
    2. 判断是否会话首条消息（用于自动生成标题）
    3. 创建 event_generator()，内部调用 agent_manager.astream()
    4. 按段（segment）追踪响应 —— 每次工具执行后模型重新生成文本即开启新段
    5. done 到达后：保存用户消息 + 每段助手消息
    6. 首条消息额外生成 ≤10 字中文标题
    7. 追加一条每日工作日志（由后端写，Agent 不负责）

SSE 事件：retrieval / reasoning / token / tool_start / tool_end
         audit_warning / new_response / done / title / error
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from config import get_settings
from graph.agent import agent_manager
from graph.daily_log import append_turn

logger = logging.getLogger("airclaw.api.chat")

router = APIRouter()

# 工具调用参数里代表「产出了文件」的字段。read_file 的 file_path 不算产出物
_ARTIFACT_FIELD = {"write_file": "file_path", "terminal": "out_dir"}


def _artifacts(segments: list[dict]) -> list[str]:
    """从本轮工具调用中提取产出物路径，去重保序。"""
    found: list[str] = []
    for segment in segments:
        for call in segment["tool_calls"]:
            field = _ARTIFACT_FIELD.get(call.get("tool", ""))
            args = call.get("input")
            value = args.get(field) if field and isinstance(args, dict) else None
            if value:
                found.append(str(value))
    return list(dict.fromkeys(found))


class ChatRequest(BaseModel):
    message: str = Field(description="用户消息")
    session_id: str = Field(description="会话 ID")
    stream: bool = Field(default=True, description="是否流式（当前仅支持流式）")

    # 刻意不接受客户端传来的操作者标识：身份由部署配置（APP_USER）决定，
    # 客户端可伪造的字段不能进审计日志，否则它只是看起来在追溯责任。


def _sse(event: dict) -> dict:
    """把内部事件转成 SSE 帧：event 名 = 事件类型，data = 完整 JSON。"""
    event_type = event.get("type", "message")
    return {"event": event_type, "data": json.dumps(event, ensure_ascii=False)}


@router.post("/chat")
async def chat(request: ChatRequest) -> EventSourceResponse:
    sessions = agent_manager.sessions

    if not sessions.exists(request.session_id):
        raise HTTPException(status_code=404, detail=f"会话不存在：{request.session_id}")

    async def event_generator():
        history = sessions.load_for_agent(request.session_id)
        is_first_message = len(sessions.load_messages(request.session_id)) == 0

        # 按段追踪：工具执行后模型重新生成文本时开启新段
        segments: list[dict] = [{"content": [], "tool_calls": []}]
        failed = False

        async for event in agent_manager.astream(
            message=request.message,
            history=history,
            session_id=request.session_id,
            user=get_settings().app_user,
        ):
            kind = event.get("type")

            if kind == "token":
                segments[-1]["content"].append(event.get("content", ""))
            elif kind == "new_response":
                if segments[-1]["content"] or segments[-1]["tool_calls"]:
                    segments.append({"content": [], "tool_calls": []})
            elif kind == "tool_end":
                segments[-1]["tool_calls"].append(
                    {
                        "tool": event.get("tool"),
                        "input": event.get("input"),
                        "output": event.get("output"),
                    }
                )
            elif kind == "error":
                failed = True

            yield _sse(event)

        if failed:
            # 失败时不落盘半截对话，避免下一次请求读到悬空的 user 消息
            return

        # ---- 落盘：用户消息 + 每段助手消息 ----
        sessions.save_message(request.session_id, "user", request.message)
        for segment in segments:
            content = "".join(segment["content"]).strip()
            if content or segment["tool_calls"]:
                sessions.save_message(
                    request.session_id, "assistant", content, segment["tool_calls"] or None
                )

        # ---- 首条消息自动生成标题 ----
        if is_first_message:
            answer = "".join("".join(s["content"]) for s in segments).strip()
            title = await agent_manager.generate_title(request.message, answer)
            sessions.rename(request.session_id, title)
            yield _sse(
                {"type": "title", "session_id": request.session_id, "title": title}
            )

        # ---- 每日日志：由后端追加，Agent 不负责（见 graph/daily_log.py） ----
        # 放在标题生成之后，日志里才能带上真实的会话标题而非「新会话」
        append_turn(
            get_settings().memory_path / "logs",
            session_id=request.session_id,
            session_title=sessions.load(request.session_id).get("title", ""),
            user_message=request.message,
            reply="".join("".join(s["content"]) for s in segments).strip(),
            artifacts=_artifacts(segments),
        )

    return EventSourceResponse(event_generator())
