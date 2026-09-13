"""Embedding 服务接入。

两类服务的接口形态不同，用同一个类按 provider 分派：

    jina    —— 公网原型。接口为 POST /v1/embeddings，除 model/input 外还需
               `task` 参数区分用途。**实测同一文本用 retrieval.query 与
               retrieval.passage 得到的向量余弦相似度仅 0.87**，即两者不可互换：
               建索引必须用 passage，检索必须用 query。

    openai  —— 内网 BGE-M3 等标准 OpenAI 兼容服务。只传 model/input。

两者响应格式一致（OpenAI 的 `data[].embedding`），差别仅在请求体。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx
from llama_index.core.embeddings import BaseEmbedding
from pydantic import Field, PrivateAttr

from config import get_settings

logger = logging.getLogger("airclaw.embedding")

BATCH_SIZE = 16


class InternalEmbedding(BaseEmbedding):
    """接入内网或公网的 embedding 服务。"""

    model_name: str = Field(default="")
    base_url: str = Field(default="")
    api_key: str = Field(default="")
    provider: str = Field(default="openai")
    task_query: str = Field(default="retrieval.query")
    task_text: str = Field(default="retrieval.passage")
    timeout: int = Field(default=60)

    _client: httpx.Client = PrivateAttr()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = httpx.Client(
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    # ---- 内部：一次请求 ----

    def _embed(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        if not texts:
            return []

        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            body: dict[str, Any] = {"model": self.model_name, "input": batch}

            if self.provider == "jina":
                body["task"] = self.task_query if is_query else self.task_text
                body["normalized"] = True

            resp = self._client.post(self.base_url, json=body)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Embedding 服务返回 {resp.status_code}：{resp.text[:300]}"
                )
            data = resp.json().get("data", [])
            if len(data) != len(batch):
                raise RuntimeError(
                    f"Embedding 返回条数不符：请求 {len(batch)} 条，返回 {len(data)} 条"
                )
            # 服务端不保证顺序，按 index 排序后再取
            for item in sorted(data, key=lambda d: d.get("index", 0)):
                out.append(item["embedding"])
        return out

    # ---- BaseEmbedding 接口 ----

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed([query], is_query=True)[0]

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed([text], is_query=False)[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, is_query=False)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._get_text_embeddings(texts)


def build_embedding() -> InternalEmbedding:
    """按当前配置构造 embedding 实例。"""
    settings = get_settings()
    # 配置里的 URL 可能是完整端点（jina）也可能是 base（内网 OpenAI 兼容），统一规范化
    url = settings.embedding_base_url.rstrip("/")
    if not url.endswith("/embeddings"):
        url = f"{url}/embeddings"

    return InternalEmbedding(
        model_name=settings.embedding_model,
        base_url=url,
        api_key=settings.embedding_api_key,
        provider=settings.embedding_provider.lower(),
        timeout=settings.llm_request_timeout,
    )


# --------------------------------------------------------------------------
# 向量摘要 —— 供 RAG 诊断页展示
# --------------------------------------------------------------------------

HEAT_BUCKETS = 64


def vector_summary(vec: list[float], *, buckets: int = HEAT_BUCKETS) -> dict:
    """把一条向量压成可读摘要。

    1024 维浮点数组本身没法看，管理员真正需要判断的是「它正常吗」——
    全零或范数异常说明 embedding 服务有问题。故给出统计量 + 一条分段均值的色带
    （保留形态又足够紧凑，64 段足以看出有没有塌缩），再附前若干维的原始数值。
    """
    if not vec:
        return {"dim": 0, "ok": False}

    n = len(vec)
    step = max(1, n // buckets)
    heat = [
        sum(vec[i : i + step]) / len(vec[i : i + step])
        for i in range(0, n, step)
    ]

    return {
        "dim": n,
        "ok": True,
        "norm": round(math.sqrt(sum(x * x for x in vec)), 4),
        "mean": round(sum(vec) / n, 6),
        "min": round(min(vec), 6),
        "max": round(max(vec), 6),
        "preview": [round(x, 4) for x in vec[:16]],
        "heat": [round(x, 5) for x in heat],
    }
