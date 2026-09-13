"""search_knowledge_base —— 内网知识库混合检索。

对应 PRD 第二章第 4 项。当用户询问具体的知识库内容（非对话历史）时，
Agent 调用本工具做深度检索。

    语料来源：knowledge/ 目录（管理员从内网文档源离线导入，导入前须经保密审查）
    检索方式：BM25 关键词 + 向量语义，倒数排名融合（RRF）
    返回条数：top-3
    索引入口：tools/knowledge_index.py

本工具为**只读**操作，重复调用无副作用。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from tools.knowledge_index import KnowledgeIndex

TOP_K = 3


class SearchKnowledgeInput(BaseModel):
    query: str = Field(
        description="检索关键词或问句。建议保留原始术语与型号编号，不要改写或翻译。"
    )


class SearchKnowledgeTool(BaseTool):
    name: str = "search_knowledge_base"
    description: str = (
        "在内网知识库中做混合检索（关键词 BM25 + 向量语义），返回最相关的 3 个片段。\n"
        "适用场景：查询标准规范条款、型号资料参数、历史项目文档内容等具体事实。\n"
        "不适用：当前对话中已出现的内容（直接用上下文即可）、需要计算或推理的问题。\n"
        "注意：知识库只包含管理员离线导入并经保密审查的语料。"
        "若检索结果为空，应如实告知用户「知识库中未找到相关依据」，"
        "严禁凭训练记忆编造条款号、指标值或文档名。"
    )
    args_schema: type[BaseModel] = SearchKnowledgeInput

    # 只读操作，可安全去重
    idempotent: bool = True

    _index: KnowledgeIndex = PrivateAttr()

    def __init__(self, index: KnowledgeIndex, **kwargs) -> None:
        super().__init__(**kwargs)
        self._index = index

    def _run(self, query: str, run_manager=None) -> str:
        try:
            results, mode = self._index.retrieve(query, top_k=TOP_K)
        except Exception as exc:  # noqa: BLE001
            return f"[检索失败] {type(exc).__name__}: {exc}\n请如实告知用户检索未完成。"

        if not results:
            stats = self._index.stats()
            if stats["corpus"]["count"] == 0:
                return (
                    "知识库为空。knowledge/ 目录中尚无任何语料，"
                    "需由管理员从内网文档源离线导入并经保密审查后重试。"
                )
            return f"知识库中未找到与「{query}」相关的内容。"

        mode_label = {
            "hybrid": "混合检索（BM25 + 向量）",
            "bm25": "纯关键词检索（向量服务不可用，已降级）",
        }.get(mode, mode)

        lines = [f"检索方式：{mode_label}；命中 {len(results)} 条。\n"]
        for i, item in enumerate(results, 1):
            lines.append(f"【结果 {i}】来源：{item['source']}")
            lines.append(item["text"].strip())
            lines.append("")
        return "\n".join(lines)
