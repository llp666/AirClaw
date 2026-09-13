"""Agent 引擎。

核心单例类 AgentManager，管理 Agent 的生命周期与流式调用。

对应 README「Agent 引擎 graph/」：

    initialize(base_dir)      创建内网 LLM、加载工具列表与审计钩子、初始化 SessionManager
    _build_agent()            每次调用都重建，确保读取最新的 System Prompt 与 RAG 配置
    astream(message, history) 核心流式方法，依次 yield 各类事件

事件序列：

    [RAG 模式] retrieval → token... → tool_start → tool_end → new_response → token... → done
    [普通模式]                    token... → tool_start → tool_end → new_response → token... → done

    拦截发生时 tool_start 之后推送 audit_warning，该次工具调用被阻断。

关于「每次请求重建 Agent」：System Prompt 由 workspace/ 下的 Markdown 文件与
memory/MEMORY.md 实时拼接而成，重建才能保证用户在右侧编辑器中的修改立即生效。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from langchain.agents import create_agent

from config import BACKEND_DIR, get_runtime_config, get_settings
from graph.audit_hooks import AuditBus, AuditLog, AuditMiddleware, RuleEngine
from graph.llm import REASONING_KEY, build_llm
from graph.memory_indexer import MemoryIndexer
from graph.prompt_builder import build_system_prompt, uses_memory_retrieval
from graph.session_manager import SessionManager
from graph.tool_dedup import ToolDedupMiddleware
from tools import get_all_tools
from tools.knowledge_index import KnowledgeIndex

logger = logging.getLogger("airclaw.agent")

TITLE_PROMPT = (
    "请为下面这轮对话拟一个中文标题，用于会话列表展示。\n"
    "要求：不超过 10 个字，直接输出标题本身，不要引号、不要标点结尾、不要任何解释。\n\n"
    "用户：{user}\n助手：{assistant}"
)

SUMMARY_PROMPT = (
    "请把下面的对话历史压缩成一段中文摘要，用于后续对话的上下文。\n"
    "要求：不超过 500 字；保留关键事实、结论、尚未完成的事项与已达成的约定；"
    "使用第三人称陈述，不要写成对话形式。\n\n对话历史：\n{history}"
)

# 注入检索结果时用的前缀，与 prompt_builder.RAG_GUIDANCE 里告知模型的写法一致
MEMORY_CONTEXT_MARK = "[记忆检索结果]"


def _memory_context(results: list[dict]) -> str:
    """把检索到的记忆片段渲染成临时的上下文块。

    以 user 角色追加在真正的用户消息之前（history 尾部）。OpenAI 兼容接口接受
    连续两条 user 消息，且这样比塞进 system prompt 更贴近「本轮才出现的信息」的语义。
    该块仅当次请求可见，不写入会话文件。
    """
    body = "\n\n---\n\n".join(r["text"] for r in results)
    return f"{MEMORY_CONTEXT_MARK}\n以下是从长期记忆中检索到的片段，供参考：\n\n{body}"


class AgentManager:
    def __init__(self) -> None:
        self._settings = None
        self._sessions: SessionManager | None = None
        self._rules: RuleEngine | None = None
        self._audit: AuditLog | None = None
        self._knowledge: KnowledgeIndex | None = None
        self._memory: MemoryIndexer | None = None
        self._tools: list = []

    # ---- 生命周期 ----

    def initialize(self, base_dir: Path | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._sessions = SessionManager(settings.sessions_path)
        self._rules = RuleEngine(settings.secrecy_rules_path)
        self._audit = AuditLog(settings.audit_path)
        # 索引实例由 AgentManager 持有，便于语料变更后主动重建
        self._knowledge = KnowledgeIndex(settings)
        self._memory = MemoryIndexer(settings)
        self._tools = get_all_tools(
            base_dir or BACKEND_DIR.parent, knowledge_index=self._knowledge
        )

        logger.info(
            "AgentManager 初始化完成：工具 %d 个，审查规则 %d 条",
            len(self._tools),
            self._rules.rule_count,
        )

    def rebuild_knowledge_index(self, *, force: bool = True) -> dict:
        """重建知识库索引。语料上传/删除后调用，使检索立即反映变更。"""
        if self._knowledge is None:
            raise RuntimeError("AgentManager 尚未初始化")
        return self._knowledge.build(force=force)

    def rebuild_memory_index(self, *, force: bool = True) -> dict:
        """重建记忆索引。MEMORY.md 变更后调用。"""
        if self._memory is None:
            raise RuntimeError("AgentManager 尚未初始化")
        return self._memory.rebuild_index(force=force)

    @property
    def sessions(self) -> SessionManager:
        if self._sessions is None:
            raise RuntimeError("AgentManager 尚未初始化")
        return self._sessions

    @property
    def knowledge_index(self) -> KnowledgeIndex:
        if self._knowledge is None:
            raise RuntimeError("AgentManager 尚未初始化")
        return self._knowledge

    @property
    def memory_index(self) -> MemoryIndexer:
        if self._memory is None:
            raise RuntimeError("AgentManager 尚未初始化")
        return self._memory

    def _build_agent(self, bus: AuditBus, session_id: str, user: str, rag_mode: bool):
        """重建 Agent。每次调用都重建，以反映最新的 System Prompt 与 RAG 配置。"""
        settings = self._settings or get_settings()
        prompt = build_system_prompt(settings, rag_mode=rag_mode)
        # middleware 顺序即嵌套顺序：第一个在最外层。
        # 审计必须最外层，才能看到（并记录）所有调用，包括被去重拦下的那些。
        middleware = [
            AuditMiddleware(
                engine=self._rules,
                audit_log=self._audit,
                bus=bus,
                session_id=session_id,
                user=user,
            ),
            ToolDedupMiddleware(enabled=settings.tool_dedup_enabled),
        ]
        return create_agent(
            model=build_llm(),
            tools=self._tools,
            system_prompt=prompt,
            middleware=middleware,
            name="airclaw",
        )

    # ---- 流式调用 ----

    async def astream(
        self,
        message: str,
        history: list[dict],
        session_id: str,
        user: str | None = None,
    ) -> AsyncIterator[dict]:
        """执行一轮对话，逐事件 yield。

        user 为 None 时取部署配置的操作者身份（见 Settings.app_user）。
        事件类型：token / reasoning / tool_start / tool_end / audit_warning
                 / new_response / done / retrieval / error
        """
        user = user or (self._settings or get_settings()).app_user
        rag_mode = get_runtime_config().get_rag_mode()
        settings = self._settings or get_settings()
        bus = AuditBus()

        # RAG 模式且 MEMORY.md 够大时才检索。判据与 prompt_builder 共用：
        # 两处必须一致，否则会出现「按检索模式组装 prompt、却没做检索」，
        # 结果是这一轮完全没有长期记忆——正是这个 bug 曾经的样子。
        if uses_memory_retrieval(settings, rag_mode=rag_mode):
            results, mode = await asyncio.to_thread(
                self._memory.retrieve, message, settings.memory_retrieval_top_k
            )
            event = {"type": "retrieval", "query": message, "results": results, "mode": mode}
            if mode == "low_relevance":
                # 检索确实执行了，只是没有候选达到相关度下限。与「记忆为空」不同，
                # 得说清楚，否则用户看到「命中 0 条」会以为检索坏了。
                event["note"] = (
                    f"记忆索引里没有任何条目的相关度达到下限"
                    f"（{settings.retrieval_min_score}），判定为记忆中无相关内容。"
                )
            yield event
            if results:
                # 仅用于当次请求，不落盘到会话文件
                history = [
                    *history,
                    {"role": "user", "content": _memory_context(results)},
                ]

        agent = self._build_agent(bus, session_id, user, rag_mode)

        inputs = {"messages": [*history, {"role": "user", "content": message}]}

        full_text: list[str] = []
        tool_calls: list[dict] = []
        # 模型会并行发起多个工具调用，故按 run_id 分别追踪，不能用单个变量
        pending: dict[str, dict] = {}
        last_was_tool = False

        try:
            async for event in agent.astream_events(
                inputs,
                version="v2",
                # LangGraph 默认步数上限 25，多步技能（如 code_test 六步带调试往返）
                # 会撞上限中断整轮，故放宽，见 config.Settings.agent_recursion_limit
                config={"recursion_limit": (self._settings or get_settings()).agent_recursion_limit},
            ):
                name = event["event"]

                # 每次工具执行完毕后模型重新生成文本 —— 通知前端开新的气泡
                if name == "on_chat_model_start":
                    if last_was_tool:
                        last_was_tool = False
                        yield {"type": "new_response"}

                elif name == "on_chat_model_stream":
                    chunk = event["data"].get("chunk")
                    if chunk is None:
                        continue
                    reasoning = (getattr(chunk, "additional_kwargs", {}) or {}).get(
                        REASONING_KEY
                    )
                    if reasoning:
                        yield {"type": "reasoning", "content": reasoning}
                    content = chunk.content
                    if content:
                        full_text.append(content)
                        yield {"type": "token", "content": content}

                elif name == "on_tool_start":
                    tool_name = event.get("name", "unknown")
                    tool_input = event["data"].get("input")
                    pending[event["run_id"]] = {"tool": tool_name, "input": tool_input}
                    yield {"type": "tool_start", "tool": tool_name, "input": tool_input}

                elif name == "on_tool_end":
                    output = event["data"].get("output")
                    text = getattr(output, "content", output)
                    text = text if isinstance(text, str) else str(text)
                    started = pending.pop(event["run_id"], {})
                    tool_name = started.get("tool", event.get("name", "unknown"))
                    tool_input = started.get("input")
                    tool_calls.append(
                        {"tool": tool_name, "input": tool_input, "output": text}
                    )
                    last_was_tool = True
                    yield {
                        "type": "tool_end",
                        "tool": tool_name,
                        "input": tool_input,
                        "output": text,
                    }

                # 审查拦截发生在工具执行内部：被阻断的调用不会产生
                # on_tool_start/on_tool_end，因此不能只在工具分支里取告警。
                # 每轮事件后统一 drain，保证 audit_warning 即时推送而非堆积到最后。
                for warning in bus.drain():
                    yield warning

        except Exception as exc:  # noqa: BLE001 — 需要把任何异常转成 SSE error 事件
            logger.exception("Agent 流式执行失败")
            yield {"type": "error", "error": f"{type(exc).__name__}: {exc}"}
            return

        # 兜底：审计告警可能在最后一个事件之后才产生
        for warning in bus.drain():
            yield warning

        yield {
            "type": "done",
            "content": "".join(full_text),
            "tool_calls": tool_calls,
            "session_id": session_id,
        }

    # ---- 辅助：标题与摘要 ----

    async def _complete(self, prompt: str, max_tokens: int = 512) -> str:
        """工具性 LLM 调用（标题、摘要），关闭思考以避免正文被推理吞掉。"""
        llm = build_llm(thinking=False)
        result = await llm.ainvoke(prompt, max_tokens=max_tokens)
        text = result.content if isinstance(result.content, str) else str(result.content)
        return text.strip()

    async def generate_title(self, user_message: str, assistant_message: str) -> str:
        """生成 ≤10 字的中文会话标题。失败时回落为截断的用户消息。"""
        try:
            title = await self._complete(
                TITLE_PROMPT.format(
                    user=user_message[:500], assistant=assistant_message[:500]
                ),
                max_tokens=64,
            )
            title = title.strip().strip("《》\"'。.：:").splitlines()[0].strip()
            return title[:20] or user_message[:10]
        except Exception as exc:  # noqa: BLE001
            logger.warning("标题生成失败，回落到截断用户消息：%s", exc)
            return user_message.strip().splitlines()[0][:12] or "新会话"

    async def summarize(self, history: list[dict]) -> str:
        """把对话历史压缩成 ≤500 字的中文摘要。"""
        rendered = "\n".join(
            f"{m['role']}: {m.get('content', '')[:1000]}" for m in history
        )
        try:
            return (await self._complete(
                SUMMARY_PROMPT.format(history=rendered), max_tokens=1024
            )).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("摘要生成失败：%s", exc)
            return f"（摘要生成失败：{type(exc).__name__}）原始消息已归档，共 {len(history)} 条。"


# 模块级单例
agent_manager = AgentManager()
