"""会话管理接口。

    GET    /api/sessions                    列出全部会话（按更新时间倒序）
    POST   /api/sessions                    创建新会话（UUID 命名）
    PUT    /api/sessions/{id}               重命名
    DELETE /api/sessions/{id}               删除
    GET    /api/sessions/{id}/messages      完整消息（含 System Prompt）
    GET    /api/sessions/{id}/history       对话历史（不含 System Prompt，含 tool_calls）
    POST   /api/sessions/{id}/generate-title  AI 生成标题
    POST   /api/sessions/{id}/compress      压缩前 50% 历史消息
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.demo_guard import visitor_id
from config import get_runtime_config, get_settings
from graph.agent import agent_manager
from graph.prompt_builder import build_system_prompt, describe_components

logger = logging.getLogger("airclaw.api.sessions")

router = APIRouter()


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


def _sessions():
    return agent_manager.sessions


def _own(session_id: str, request: Request) -> None:
    """演示模式：只认本访客自己的会话。

    越权时返回 404 而不是 403——403 等于告诉对方「这个 ID 确实存在，只是不属于你」，
    而演示面向的是不特定访客，没必要透露这个信息。历史遗留的、没有 owner 的会话
    （本机自己用出来的）对访客同样不可见。
    """
    settings = get_settings()
    if not settings.demo_mode:
        return
    if _sessions().owner(session_id) != visitor_id(request):
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")


def _require(session_id: str, request: Request):
    sessions = _sessions()
    if not sessions.exists(session_id):
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    _own(session_id, request)
    return sessions


def _demo() -> bool:
    return get_settings().demo_mode


@router.get("/sessions")
async def list_sessions(request: Request) -> dict:
    owner = visitor_id(request) if _demo() else None
    return {"sessions": _sessions().list_sessions(owner=owner)}


@router.post("/sessions", status_code=201)
async def create_session(request: Request) -> dict:
    return _sessions().create(owner=visitor_id(request) if _demo() else "")


@router.put("/sessions/{session_id}")
async def rename_session(session_id: str, body: RenameRequest, request: Request) -> dict:
    sessions = _require(session_id, request)
    sessions.rename(session_id, body.title)
    return {"id": session_id, "title": body.title}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict:
    sessions = _require(session_id, request)
    sessions.delete(session_id)
    return {"id": session_id, "deleted": True}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, request: Request) -> dict:
    """完整消息，含当前生效的 System Prompt（供前端 Raw Messages 视图）。"""
    sessions = _require(session_id, request)
    data = sessions.load(session_id)
    settings = get_settings()
    rag_mode = get_runtime_config().get_rag_mode()
    return {
        "session_id": session_id,
        "system_prompt": build_system_prompt(settings, rag_mode=rag_mode),
        "system_prompt_components": describe_components(settings, rag_mode=rag_mode),
        "messages": data.get("messages", []),
        "compressed_context": data.get("compressed_context", ""),
    }


@router.get("/sessions/{session_id}/history")
async def get_history(session_id: str, request: Request) -> dict:
    sessions = _require(session_id, request)
    return {"session_id": session_id, "messages": sessions.load_messages(session_id)}


@router.post("/sessions/{session_id}/generate-title")
async def generate_title(session_id: str, request: Request) -> dict:
    sessions = _require(session_id, request)
    messages = sessions.load_messages(session_id)
    if not messages:
        raise HTTPException(status_code=400, detail="会话为空，无法生成标题")

    first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
    first_assistant = next(
        (m["content"] for m in messages if m["role"] == "assistant"), ""
    )
    title = await agent_manager.generate_title(first_user, first_assistant)
    sessions.rename(session_id, title)
    return {"session_id": session_id, "title": title}


@router.post("/sessions/{session_id}/compress")
async def compress_session(session_id: str, request: Request) -> dict:
    """压缩前 50% 的历史消息（至少 4 条）。"""
    sessions = _require(session_id, request)
    messages = sessions.load_messages(session_id)

    if len(messages) < 4:
        raise HTTPException(
            status_code=400, detail=f"消息不足 4 条（当前 {len(messages)}），无需压缩"
        )

    ratio = get_settings().compress_ratio
    take = max(4, int(len(messages) * ratio))
    take = min(take, len(messages) - 1)  # 至少留一条，避免压完为空

    summary = await agent_manager.summarize(messages[:take])
    result = sessions.compress_history(session_id, summary, take)

    return {
        "session_id": session_id,
        "summary": summary,
        **result,
    }
