"""RAG 诊断接口 —— 供入库管理员查看检索召回了什么、为什么。

    GET  /api/rag/status     两个索引的状态（分块数、向量是否可用、当前阈值等）
    POST /api/rag/inspect    跑一次检索，返回全部候选（含被阈值挡掉的）及其分数与向量

## 为什么需要它

相关性下限、切块粒度、两路召回的比例，这些参数光看代码或日志调不出来。管理员真正
需要的是**每一条候选的原始分数**，尤其是被挡掉的那一批——阈值卡得松还是紧，只有看到
「差多少分被挡下」才能判断。故本接口刻意不做过滤，全量返回。

分数由索引器的 `inspect()` 产出，而它与生产检索共用同一条 `_rank` 路径，故诊断页
看到的数字与实际检索完全一致——若诊断页另算一套，管理员据此调的阈值就会和生产对不上。

## 边界

页面本身**不做身份验证**：系统没有登录体系（见 README「操作者身份与审计目录」），
能打开前端的人都能访问这个页面。它依赖部署侧的网络隔离，或由保密管理员在外层加网关。
这一点在页面上也有明示，不要把它当成受控后台。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import get_settings
from graph.agent import agent_manager

logger = logging.getLogger("airclaw.api.rag")

router = APIRouter()

#: 允许诊断的语料。名字对应 AgentManager 持有的两个索引实例
CORPORA = ("knowledge", "memory")


class InspectRequest(BaseModel):
    corpus: str = Field(default="knowledge", description=f"语料：{' / '.join(CORPORA)}")
    query: str = Field(description="要检索的查询，与生产检索的输入一致")
    top_k: int = Field(default=8, ge=1, le=50, description="返回多少条候选")


@router.get("/rag/status")
async def rag_status() -> dict:
    """两个索引的当前状态。诊断页首屏据此渲染概况。"""
    settings = get_settings()
    memory = agent_manager.memory_index
    knowledge = agent_manager.knowledge_index

    return {
        "min_score": settings.retrieval_min_score,
        "embedding": {
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "dimensions": settings.embedding_dimensions,
        },
        "memory": {
            **memory.stats(),
            "inline_max_chars": settings.memory_inline_max_chars,
            "inline_ok": settings.memory_inline_ok(),
            "retrieval_top_k": settings.memory_retrieval_top_k,
        },
        "knowledge": knowledge.stats(),
    }


@router.post("/rag/inspect")
async def rag_inspect(request: InspectRequest) -> dict:
    """跑一次检索并返回全部候选。不改变任何状态，可反复调用。"""
    if request.corpus not in CORPORA:
        raise HTTPException(
            status_code=400, detail=f"corpus 取值须为 {' 或 '.join(CORPORA)}"
        )
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")

    index = (
        agent_manager.memory_index
        if request.corpus == "memory"
        else agent_manager.knowledge_index
    )

    started = time.perf_counter()
    try:
        result = index.inspect(request.query, top_k=request.top_k)
    except Exception as exc:  # noqa: BLE001 — 诊断接口要如实回报失败原因
        logger.exception("RAG 诊断失败")
        raise HTTPException(status_code=500, detail=f"检索失败：{exc}") from exc

    return {
        "corpus": request.corpus,
        "query": request.query,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        **result,
    }
