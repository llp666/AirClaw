"""Token 统计接口。

    GET  /api/tokens/session/{id}   会话 Token 统计（System Prompt + 消息）
    POST /api/tokens/files          批量统计文件 Token 数

使用 tiktoken 的 cl100k_base 编码器（与 GPT-4 系列一致）。

## 内网部署注意

tiktoken 首次使用某个编码器时会**联网下载** BPE 词表
（默认取自 openaipublic.blob.core.windows.net），缓存在
`TIKTOKEN_CACHE_DIR`（未设置时为系统临时目录）。

本地部署无外网，必须在交付前把词表预先放入缓存目录并固定
`TIKTOKEN_CACHE_DIR` 指向项目内路径，否则本接口会在首次调用时失败。
`tiktoken_cache_status()` 用于在启动阶段自检这一项。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import tiktoken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import BACKEND_DIR, get_runtime_config, get_settings
from graph.agent import agent_manager
from graph.prompt_builder import build_system_prompt

logger = logging.getLogger("airclaw.api.tokens")

router = APIRouter()

ENCODING_NAME = "cl100k_base"

_encoder = None


def _enc():
    global _encoder
    if _encoder is None:
        try:
            _encoder = tiktoken.get_encoding(ENCODING_NAME)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"tiktoken 编码器 {ENCODING_NAME} 不可用（离线环境需预置词表缓存）：{exc}",
            )
        return _encoder
    return _encoder


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_enc().encode(text))


def tiktoken_cache_status() -> dict:
    """检查词表是否已在本地缓存（离线部署自检用）。"""
    cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR")
    try:
        _enc()
        available = True
        error = None
    except Exception as exc:  # noqa: BLE001
        available = False
        error = str(exc)
    return {
        "encoding": ENCODING_NAME,
        "available": available,
        "cache_dir": cache_dir or "(未设置，使用系统临时目录)",
        "error": error,
    }


class TokenFilesRequest(BaseModel):
    paths: list[str] = Field(description="相对 backend/ 的路径列表")


def _safe_path(raw: str) -> Path:
    """仅允许统计 backend/ 内、且属白名单目录的文件，与 files.py 保持同一套约束。"""
    from api.files import _resolve  # 复用同一份白名单逻辑，避免两处规则漂移

    return _resolve(raw)


@router.get("/tokens/session/{session_id}")
async def session_tokens(session_id: str) -> dict:
    sessions = agent_manager.sessions
    if not sessions.exists(session_id):
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")

    settings = get_settings()
    rag_mode = get_runtime_config().get_rag_mode()
    system_prompt = build_system_prompt(settings, rag_mode=rag_mode)

    system_tokens = count_tokens(system_prompt)
    message_tokens = sum(
        count_tokens(m.get("content", "")) for m in sessions.load_messages(session_id)
    )
    summary = sessions.get_compressed_context(session_id)

    return {
        "session_id": session_id,
        "system_tokens": system_tokens,
        "message_tokens": message_tokens,
        "summary_tokens": count_tokens(summary),
        "total_tokens": system_tokens + message_tokens,
        "message_count": len(sessions.load_messages(session_id)),
    }


@router.post("/tokens/files")
async def file_tokens(request: TokenFilesRequest) -> dict:
    files = []
    total = 0
    for raw in request.paths:
        try:
            target = _safe_path(raw)
            text = target.read_text(encoding="utf-8")
            n = count_tokens(text)
        except HTTPException as exc:
            files.append({"path": raw, "error": exc.detail})
            continue
        except Exception as exc:  # noqa: BLE001
            files.append({"path": raw, "error": f"{type(exc).__name__}: {exc}"})
            continue
        total += n
        files.append({"path": raw, "tokens": n, "chars": len(text)})

    return {"files": files, "total_tokens": total}


@router.get("/tokens/system-prompt")
async def system_prompt_tokens() -> dict:
    """System Prompt 各组件的 Token 占用，供前端展示上下文构成。"""
    settings = get_settings()
    rag_mode = get_runtime_config().get_rag_mode()
    prompt = build_system_prompt(settings, rag_mode=rag_mode)

    from graph.prompt_builder import describe_components

    components = [
        {**c, "tokens": count_tokens(Path(c["path"]).read_text(encoding="utf-8"))}
        for c in describe_components(settings, rag_mode=rag_mode)
    ]
    return {
        "total_tokens": count_tokens(prompt),
        "characters": len(prompt),
        "components": components,
        "rag_mode": rag_mode,
        "backend_dir": str(BACKEND_DIR),
    }
