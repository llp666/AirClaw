"""配置管理接口。

    GET /api/config/rag-mode    获取 RAG 模式状态
    PUT /api/config/rag-mode    切换 RAG 模式

配置持久化到 backend/config.json。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from config import get_runtime_config, get_settings

router = APIRouter()


class RagModeRequest(BaseModel):
    enabled: bool


@router.get("/config/rag-mode")
async def get_rag_mode() -> dict:
    return {"enabled": get_runtime_config().get_rag_mode()}


@router.put("/config/rag-mode")
async def set_rag_mode(request: RagModeRequest) -> dict:
    get_runtime_config().set_rag_mode(request.enabled)
    return {"enabled": request.enabled}


@router.get("/config")
async def get_config() -> dict:
    """回显当前生效的关键配置，便于确认部署指向是否正确。"""
    settings = get_settings()
    return {
        "user": settings.app_user,
        "llm_model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "llm_enable_thinking": settings.llm_enable_thinking,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "sandbox_image": settings.sandbox_image,
        "sandbox_timeout": settings.sandbox_timeout,
        "prompt_component_max_chars": settings.prompt_component_max_chars,
        "compress_ratio": settings.compress_ratio,
        "rag_mode": get_runtime_config().get_rag_mode(),
    }
