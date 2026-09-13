"""System Prompt 组装器。

按固定顺序拼接 Markdown 组件（README「System Prompt 组装」）：

    ⓪ 运行时上下文        当前日期与时间（见下）
    ① SKILLS_SNAPSHOT.md  可用技能清单（启动时由 skills_scanner 生成）
    ② workspace/SOUL.md   人格、语气、边界
    ③ workspace/IDENTITY.md  名称、风格
    ④ workspace/USER.md   用户画像
    ⑤ workspace/AGENTS.md 操作指南 & 记忆/技能协议
    ⑥ memory/MEMORY.md    跨会话长期记忆
                          （RAG 模式且文件超过 memory_inline_max_chars 时，
                            改为注入检索片段，见 uses_memory_retrieval）

每个文件内容上限 20,000 字符，超出则截断并标记 ...[truncated]。
组件之间以空行分隔，并各带一个 HTML 注释标签，便于调试时定位来源。

**关于第 ⓪ 项**：PRD 第四章规定 System Prompt 由 6 部分组成，此处多了一段运行时
上下文。原因是其余 6 项全部来自静态文件，模型既没有日历也没有时钟，只能按训练数据
的先验推断——实测把 2026-09-12 写了成 2026-07-16、把 22:51 写成 15:23。日期出现在
记忆条目标题、每日日志文件名与 code_test 的归档路径三处，时间出现在日志时间戳，
猜错等于往长期记忆里写假信息。它不属于「人格/能力/记忆」任何一类，故独立成段。

代价是 System Prompt 每次请求都不同，无法利用前缀缓存。本项目的 System Prompt 本来
就按「每次调用重建」设计（workspace 文件的编辑要立即生效），故不额外损失什么。

Agent 每次被调用时都会重新读取全部文件并重新组装，因此对 workspace/ 下文件的
编辑能够立即生效，无需重启服务。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import Settings

logger = logging.getLogger("airclaw.prompt")

TRUNCATED_MARK = "...[truncated]"

# 与审计日志保持同一时区口径（见 graph/audit_hooks.CST）
CST = timezone(timedelta(hours=8))

_WEEKDAYS = "一二三四五六日"


def runtime_context_block(now: datetime | None = None) -> str:
    """运行时上下文：当前日期与时间。now 可注入，便于测试。

    日期与时间都要给：模型既没有时钟也没有日历，只能按训练数据的先验推断。
    实测两次都错——把 2026-09-12 写成 2026-07-16，把 22:51 写成 15:23。
    """
    moment = now or datetime.now(CST)
    return (
        "<!-- Current Date -->\n"
        f"当前时间：{moment:%Y-%m-%d %H:%M}（星期{_WEEKDAYS[moment.weekday()]}，UTC+8）。\n"
        "凡涉及时间一律以此为准：记忆条目的日期标题、每日日志的文件名与时间戳"
        "（`backend/memory/logs/YYYY-MM-DD.md` 内的 `## HH:MM`）、测试报告归档路径"
        "（`backend/reports/{模块名}/{日期}/`）。"
        "**不要**依据训练数据推断现在是哪一天、几点。"
    )

# RAG 模式下替换 MEMORY.md 的引导语
RAG_GUIDANCE = """<!-- Long-term Memory (RAG 模式) -->
长期记忆（MEMORY.md）未直接注入，改为按需检索。

当你需要回忆此前会话中确立的约定、偏好或事实时，系统已在本轮对话上下文中注入了
以 `[记忆检索结果]` 开头的片段。若无相关片段，说明记忆中确实没有对应内容——
不要凭空编造记忆。

如需确认记忆中是否存在某项内容，可如实告知用户"当前检索结果中未包含该项"。"""


def uses_memory_retrieval(settings: Settings, *, rag_mode: bool) -> bool:
    """RAG 模式下是否真的走检索注入。

    文件小的时候整篇注入比检索划算——检索切块取 top-k 会丢掉一部分内容，
    见 Settings.memory_inline_max_chars。这是 prompt 组装与 agent 检索共用
    的唯一判据，两处必须一致，否则会出现「按检索模式组装 prompt、却没做检索」。
    """
    return rag_mode and not settings.memory_inline_ok()


@dataclass
class PromptComponent:
    tag: str
    path: Path
    content: str
    truncated: bool = False


def _read_component(tag: str, path: Path, max_chars: int) -> PromptComponent | None:
    """读取单个组件文件。文件缺失时跳过（不阻断启动），超长时截断。"""
    if not path.is_file():
        logger.warning("System Prompt 组件缺失，已跳过：%s", path)
        return None

    text = path.read_text(encoding="utf-8").strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + TRUNCATED_MARK
        logger.warning(
            "System Prompt 组件超长已截断：%s（原始 %d 字符，上限 %d）",
            path.name,
            len(text),
            max_chars,
        )
    return PromptComponent(tag=tag, path=path, content=text, truncated=truncated)


def build_system_prompt(settings: Settings, *, rag_mode: bool = False) -> str:
    """组装完整的 System Prompt。"""
    workspace = settings.workspace_path

    specs: list[tuple[str, Path]] = [
        ("Skills Snapshot", settings.skills_snapshot_path),
        ("Soul", workspace / "SOUL.md"),
        ("Identity", workspace / "IDENTITY.md"),
        ("User Profile", workspace / "USER.md"),
        ("Agents Guide", workspace / "AGENTS.md"),
    ]

    blocks: list[str] = [runtime_context_block()]
    for tag, path in specs:
        component = _read_component(tag, path, settings.prompt_component_max_chars)
        if component is not None:
            blocks.append(f"<!-- {component.tag} -->\n{component.content}")

    if uses_memory_retrieval(settings, rag_mode=rag_mode):
        # RAG 模式且文件够大：不注入完整 MEMORY.md，改用引导语，让 Agent 通过检索获取
        blocks.append(RAG_GUIDANCE)
    else:
        memory = _read_component(
            "Long-term Memory",
            settings.memory_path / "MEMORY.md",
            settings.prompt_component_max_chars,
        )
        if memory is not None:
            blocks.append(f"<!-- {memory.tag} -->\n{memory.content}")

    prompt = "\n\n".join(blocks)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("System Prompt 组装完成，共 %d 字符", len(prompt))
    return prompt


def describe_components(settings: Settings, *, rag_mode: bool = False) -> list[dict]:
    """返回各组件的字符数，供前端 Token 统计与调试使用。"""
    workspace = settings.workspace_path
    specs: list[tuple[str, Path]] = [
        ("Skills Snapshot", settings.skills_snapshot_path),
        ("Soul", workspace / "SOUL.md"),
        ("Identity", workspace / "IDENTITY.md"),
        ("User Profile", workspace / "USER.md"),
        ("Agents Guide", workspace / "AGENTS.md"),
    ]
    if not uses_memory_retrieval(settings, rag_mode=rag_mode):
        specs.append(("Long-term Memory", settings.memory_path / "MEMORY.md"))

    out: list[dict] = []
    for tag, path in specs:
        if path.is_file():
            size = len(path.read_text(encoding="utf-8"))
            out.append({"tag": tag, "path": str(path), "chars": size})
    return out
