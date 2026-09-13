"""工具调用去重 —— 同一轮对话内的幂等保护。

## 问题

实测模型会重复发出完全相同的工具调用：一次会话里 agnes 并行发了两次
`terminal: ls /workspace/src/backend`，沙箱**真的执行了两遍**
（审计日志两条记录，648ms 与 672ms），会话文件也记了两条相同的 tool_calls。

对只读命令这只是浪费；但对**有副作用的命令**（追加写入文件、生成报告、
归档产物）重复执行就是实打实的错误——产物会变成两份。

## 策略

去重范围限定在**单轮对话内**（一次 astream 调用）。理由是：模型在同一轮里发出
参数完全相同的两次调用，几乎必然是重复生成，而非有意为之。跨轮次不去重，
因为文件、索引等外部状态可能已经变化。

缓存键为 (工具名, 规范化参数)。命中时不重新执行，直接返回上一轮的结果，
并在返回内容中标注，让模型知道发生了去重、不会误以为执行了两次。

去重后的结果仍会经过审计钩子，在审计日志中记为 `deduplicated`。

## 失效

命中去重的前提是「输入没变」。模型改了文件再跑同一条命令，命令字符串一模一样、
结果却不同，此时返回缓存就是错的。实测踩到两次：

  1. 模型在测试文件还不存在时跑了一次 `airclaw-test`（失败，结果入缓存），随后
     写入测试文件、重跑同一条命令，拿到的是缓存里那份「文件不存在」的旧结果，
     于是以为文件没写成功，反复重试，把 code_test 卡成死循环。
  2. 模型跑完测试后再次 `read_file` 那个 pytest.log，拿到的是**上一次运行**的
     缓存内容，于是认定「日志没更新、测试没生效」。

规则因此是：**任何可能改变项目状态的工具执行后，清空本轮缓存。**

曾有一版只作废「只读工具」的缓存、保留 terminal 自己的缓存，结果漏掉了最关键的一种：
模型跑测试 → 改测试文件 → 重跑同一条命令，命令字符串没变但状态变了，拿到的还是旧结果。
模型于是认定封装脚本失效，绕开它直接跑裸 pytest（实测 4 次），归档日志也被它自己覆盖了。

清空整个缓存不会削弱本模块要防的场景——模型并行发出的完全相同的调用由 `_inflight`
合并，不经过缓存；而**顺序**发出的相同调用之间只要没有状态变更，缓存依然命中。

## 关闭方式

设置 TOOL_DEDUP_ENABLED=false。关闭后所有调用都会真实执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger("airclaw.dedup")

DEDUP_MARK = "[去重命中，本轮的相同调用已执行过，未重复执行]"

#: 可能改变项目状态的工具：执行后本轮缓存全部作废
STATE_TOOLS = frozenset({"write_file", "terminal", "python_repl"})


class ToolDedupMiddleware(AgentMiddleware):
    """单轮对话内的工具调用去重。

    每次 _build_agent() 都会新建实例，因此 cache 的生命周期天然就是一轮对话。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__()
        self._enabled = enabled
        self._cache: dict[str, str] = {}
        # 正在执行中的调用。并行的相同调用必须等首次执行完成，否则两个协程
        # 都会在对方写入缓存之前查到「未命中」，双双真实执行 —— 这正是实测中
        # search_knowledge_base 被执行 4 次的原因。
        self._inflight: dict[str, asyncio.Event] = {}
        self._hits = 0

    # ---- 内部 ----

    @staticmethod
    def _key(tool_name: str, args: Any) -> str:
        try:
            canon = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            canon = str(args)
        return f"{tool_name}::{canon}"

    def _invalidate(self, tool_name: str) -> None:
        """状态工具执行成功后，清空本轮缓存——此前记下的一切都不再可信。"""
        if tool_name not in STATE_TOOLS or not self._cache:
            return
        logger.info("状态工具 %s 执行成功，清空本轮去重缓存（%d 项）",
                    tool_name, len(self._cache))
        self._cache.clear()

    def _store(self, tool_name: str, args: Any, result: Any) -> None:
        if not self._enabled or result is None:
            return
        text = getattr(result, "content", result)
        if not isinstance(text, str):
            text = str(text)
        self._invalidate(tool_name)
        self._cache[self._key(tool_name, args)] = text

    @staticmethod
    def _message(request, cached: str) -> ToolMessage:
        return ToolMessage(
            content=f"{DEDUP_MARK}\n\n{cached}",
            tool_call_id=request.tool_call.get("id", ""),
            name=request.tool_call.get("name", ""),
        )

    @property
    def hits(self) -> int:
        return self._hits

    # ---- 拦截点 ----

    def wrap_tool_call(self, request, handler):
        if not self._enabled:
            return handler(request)

        tool_call = request.tool_call
        name, args = tool_call.get("name", ""), tool_call.get("args")
        key = self._key(name, args)

        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            logger.info("工具调用去重命中（同步）：%s", key[:120])
            return self._message(request, cached)

        result = handler(request)
        self._store(name, args, result)
        return result

    async def awrap_tool_call(self, request, handler):
        if not self._enabled:
            return await handler(request)

        tool_call = request.tool_call
        name, args = tool_call.get("name", ""), tool_call.get("args")
        key = self._key(name, args)

        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            logger.info("工具调用去重命中：%s", key[:120])
            return self._message(request, cached)

        event = self._inflight.get(key)
        if event is not None:
            # 已有完全相同的调用在执行，等它完成，不重复执行
            self._hits += 1
            logger.info("工具调用合并到进行中的相同调用：%s", key[:120])
            await event.wait()
            cached = self._cache.get(key)
            if cached is not None:
                return self._message(request, cached)
            # 首次调用失败未写缓存，退化为自行执行
        else:
            event = asyncio.Event()
            self._inflight[key] = event
            try:
                result = await handler(request)
                self._store(name, args, result)
                return result
            finally:
                event.set()
                self._inflight.pop(key, None)

        result = await handler(request)
        self._store(name, args, result)
        return result
