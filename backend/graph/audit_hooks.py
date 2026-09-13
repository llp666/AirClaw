"""审计钩子 —— 审查与行为追溯。

对应 PRD 第八章 3「审计钩子机制」：在 Agent 工具调用链路上设置 pre_hook 与
post_hook 两级钩子，**所有 Core Tools 调用必须经过，不可绕过**。

挂载方式说明：
    PRD 要求「不可绕过」。LangChain 1.x 的 `create_agent` 提供原生 middleware 机制
    （`AgentMiddleware.wrap_tool_call` / `awrap_tool_call`），它在工具节点内部执行，
    Agent 自身无法跳过。相比逐个包装工具的 _run，这是更强的保证，因此采用它。

    Agent 每次请求都会重建，故 AuditMiddleware 可以按请求实例化，并为本次请求
    注入独立的告警通道 AuditBus，由 api/chat.py 读取后转成 SSE audit_warning 事件。

pre_hook（审查拦截）：
    扫描工具调用参数（命令行内容、代码片段、文件路径、检索关键词），
    规则来自 rules/secrecy_rules.json，按 action 分两类：
      block —— 阻断本次调用，推送 audit_warning，写审计日志
      flag  —— 放行，仅写审计日志并附告警标记

post_hook（结果记录）：
    记录工具执行状态、耗时与结果摘要（脱敏截断），供事后追溯。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from graph.tool_dedup import DEDUP_MARK

logger = logging.getLogger("airclaw.audit")

# 审计日志中的参数摘要上限（脱敏截断）
DIGEST_LIMIT = 200
# 结果摘要上限
RESULT_LIMIT = 300

CST = timezone(timedelta(hours=8))


@dataclass
class RuleHit:
    rule_id: str
    rule_name: str
    action: str
    message: str
    matched: str
    field: str


# --------------------------------------------------------------------------
# 参数分类：让规则只扫它该扫的东西
# --------------------------------------------------------------------------
#
# 规则库顶层声明了 match_targets = ["command", "code", "path", "query"]，
# 但早期实现扫描的是工具调用的**全部参数**，等于把 write_file 的 content
# （即将写入的文档正文）也塞给了这些面向「即将执行的命令」的规则。实测后果：
#
#   报告里写一句「审计日志位于 backend/audit/audit_20260912.jsonl」
#     → 被判为「审计日志篡改尝试」而拒写（实测连拦 7 次，code_test 第六步归档写不出来）
#   测试用例里含 'rm -rf /' 这种**测试数据**
#     → 被判为「破坏性命令」而拒写
#
# 故按工具的参数语义分类，规则可声明 targets 限定适用范围；未声明的规则
# 默认只扫 match_targets 里那四类，不扫文档内容。密级标识类规则需要扫
# content（往文件里写密级标识正是要拦的行为），在规则库里显式声明。
ARG_TARGETS: dict[str, dict[str, str]] = {
    "terminal": {"command": "command", "out_dir": "path"},
    "python_repl": {"code": "code"},
    "read_file": {"file_path": "path"},
    "write_file": {"file_path": "path", "content": "content"},
    "search_knowledge_base": {"query": "query"},
}

#: 规则未声明 targets 时适用的类别，取自规则库顶层的 match_targets
DEFAULT_TARGETS = ("command", "code", "path", "query")


@dataclass
class AuditBus:
    """本次请求的告警通道。api/chat.py 从中取出 audit_warning 事件。"""

    events: list[dict] = field(default_factory=list)

    def emit(self, payload: dict) -> None:
        self.events.append(payload)

    def drain(self) -> list[dict]:
        out, self.events = self.events, []
        return out


# --------------------------------------------------------------------------
# 规则引擎
# --------------------------------------------------------------------------

class RuleEngine:
    """加载 rules/secrecy_rules.json 并对工具参数做匹配。"""

    def __init__(self, rules_path: Path) -> None:
        self._path = rules_path
        self._rules: list[dict] = []
        self.reload()

    def reload(self) -> None:
        if not self._path.is_file():
            logger.warning("审查规则库不存在：%s（审查将不生效）", self._path)
            self._rules = []
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._rules = data.get("rules", [])
            logger.info("审查规则库已加载：%d 条规则", len(self._rules))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("审查规则库加载失败：%s（审查将不生效）", exc)
            self._rules = []

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def check(self, params: dict[str, Any], tool_name: str = "") -> list[RuleHit]:
        """扫描工具调用参数，返回全部命中项。

        tool_name 用于判定每个参数的类别，进而只套用适用于该类别的规则。
        工具未知时按最保守方式处理：不做类别过滤，所有规则都扫。
        """
        arg_map = ARG_TARGETS.get(tool_name)
        hits: list[RuleHit] = []

        for field_name, raw in params.items():
            if not isinstance(raw, str):
                raw = json.dumps(raw, ensure_ascii=False)
            if not raw:
                continue

            # 类别未知（工具未知，或该工具新增了参数）时不设限，宁多扫不漏扫
            kind = arg_map.get(field_name) if arg_map else None

            for rule in self._rules:
                if kind is not None and kind not in rule.get("targets", DEFAULT_TARGETS):
                    continue
                flags = re.IGNORECASE if rule.get("flags", "").lower() == "i" else 0
                for pattern in rule.get("patterns", []):
                    matched = None
                    if rule.get("type") == "keyword":
                        if pattern in raw:
                            matched = pattern
                    else:
                        try:
                            m = re.search(pattern, raw, flags)
                            if m:
                                matched = m.group(0)
                        except re.error as exc:
                            logger.warning("规则 %s 的正则无效：%s", rule.get("id"), exc)
                            continue

                    if matched is not None:
                        hits.append(
                            RuleHit(
                                rule_id=rule.get("id", "unknown"),
                                rule_name=rule.get("name", ""),
                                action=rule.get("action", "flag"),
                                message=rule.get("message", ""),
                                matched=matched,
                                field=field_name,
                            )
                        )
                        break  # 同一条规则命中一次即可
        return hits


# --------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------

class AuditLog:
    """按日分片的 JSONL 审计日志，只追加（append-only）。"""

    def __init__(self, audit_dir: Path) -> None:
        self._dir = audit_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self) -> Path:
        return self._dir / f"audit_{time.strftime('%Y%m%d')}.jsonl"

    def write(
        self,
        *,
        session_id: str,
        user: str,
        tool_name: str,
        params_digest: str,
        result_status: str,
        block_reason: str | None = None,
        duration_ms: int | None = None,
        result_digest: str | None = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(CST).isoformat(),
            "session_id": session_id,
            "user": user,
            "tool_name": tool_name,
            "params_digest": params_digest,
            "result_status": result_status,
            "block_reason": block_reason,
        }
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        if result_digest is not None:
            entry["result_digest"] = result_digest

        # 只追加：以 "a" 模式打开，不做任何读取-改写
        with self._path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def harden_audit_dir(audit_dir: Path) -> None:
    """收紧审计目录的文件系统权限，只对属主开放。启动时调用一次。

    对应 PRD 第八章 4「审计目录访问权限受控，仅保密管理员与运维角色可读取，
    普通用户不可通过前端或文件接口访问」。接口层已经把 audit/ 排除在文件白名单
    之外（见 api/files.py），这里补上文件系统那一层。

    限 POSIX：Windows 上 os.chmod 只影响只读位，管不住 ACL，实际约束要靠部署方
    按本单位要求配 NTFS 权限。故在 Windows 上静默跳过，不做假装有效的加固。
    """
    if os.name != "posix":
        return
    try:
        os.chmod(audit_dir, 0o700)
    except OSError as exc:
        logger.warning("审计目录权限收紧失败（%s）：%s", audit_dir, exc)


def _digest(value: Any, limit: int) -> str:
    """参数/结果摘要：序列化后截断。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------

class AuditMiddleware(AgentMiddleware):
    """pre_hook 审查拦截 + post_hook 结果记录。"""

    def __init__(
        self,
        *,
        engine: RuleEngine,
        audit_log: AuditLog,
        bus: AuditBus,
        session_id: str,
        user: str,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._log = audit_log
        self._bus = bus
        self._session_id = session_id
        self._user = user

    # -- 内部：pre_hook 判定 --

    def _pre_hook(self, tool_name: str, args: dict) -> RuleHit | None:
        """返回需要阻断的命中项；无需阻断时返回 None（flag 类命中只记日志）。"""
        hits = self._engine.check(args, tool_name)
        if not hits:
            return None

        blocker = next((h for h in hits if h.action == "block"), None)

        if blocker is not None:
            reason = f"命中审查规则 {blocker.rule_id}（{blocker.rule_name}）：{blocker.message}"
            self._log.write(
                session_id=self._session_id,
                user=self._user,
                tool_name=tool_name,
                params_digest=_digest(args, DIGEST_LIMIT),
                result_status="blocked",
                block_reason=f"{blocker.rule_id}: {blocker.message}",
            )
            self._bus.emit(
                {
                    "type": "audit_warning",
                    "tool": tool_name,
                    "reason": reason,
                    "rule_id": blocker.rule_id,
                    "severity": "high",
                    "matched": blocker.matched,
                }
            )
            logger.warning("工具调用被拦截 tool=%s rule=%s", tool_name, blocker.rule_id)
            return blocker

        # 仅 flag：放行，但留痕并告警
        flagged = hits[0]
        self._log.write(
            session_id=self._session_id,
            user=self._user,
            tool_name=tool_name,
            params_digest=_digest(args, DIGEST_LIMIT),
            result_status="flagged",
            block_reason=None,
        )
        self._bus.emit(
            {
                "type": "audit_warning",
                "tool": tool_name,
                "reason": f"命中关注规则 {flagged.rule_id}（{flagged.rule_name}）：{flagged.message}",
                "rule_id": flagged.rule_id,
                "severity": "medium",
                "matched": flagged.matched,
                "blocked": False,
            }
        )
        return None

    # -- 内部：post_hook 记录 --

    def _post_hook(self, tool_name: str, args: dict, result: Any, duration_ms: int) -> None:
        status = "executed"
        text = result if isinstance(result, str) else str(getattr(result, "content", result))
        if text.startswith("[已拦截]") or "[已被审查拦截]" in text:
            status = "blocked"
        elif text.startswith(DEDUP_MARK):
            # 被去重中间件拦下，未真实执行
            status = "deduplicated"
        elif text.startswith("[退出码") or "Traceback" in text or "执行超时" in text:
            status = "failed"

        self._log.write(
            session_id=self._session_id,
            user=self._user,
            tool_name=tool_name,
            params_digest=_digest(args, DIGEST_LIMIT),
            result_status=status,
            block_reason=None,
            duration_ms=duration_ms,
            result_digest=_digest(text, RESULT_LIMIT),
        )

    # -- 拦截点 --

    def _blocked_message(self, hit: RuleHit) -> str:
        return (
            f"[已被审查拦截] 命中规则 {hit.rule_id}（{hit.rule_name}）：{hit.message}\n"
            f"本次工具调用未执行。请如实告知用户拦截原因，等待人工判断；\n"
            f"不要改写参数后重试以规避拦截。"
        )

    def wrap_tool_call(self, request, handler):
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown")
        args = tool_call.get("args", {}) or {}

        hit = self._pre_hook(tool_name, args)
        if hit is not None:
            return ToolMessage(
                content=self._blocked_message(hit),
                tool_call_id=tool_call.get("id", ""),
                name=tool_name,
            )

        started = time.perf_counter()
        try:
            result = handler(request)
        except Exception as exc:
            self._log.write(
                session_id=self._session_id, user=self._user, tool_name=tool_name,
                params_digest=_digest(args, DIGEST_LIMIT), result_status="failed",
                block_reason=f"{type(exc).__name__}: {exc}"[:200],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        self._post_hook(tool_name, args, result, int((time.perf_counter() - started) * 1000))
        return result

    async def awrap_tool_call(self, request, handler):
        tool_call = request.tool_call
        tool_name = tool_call.get("name", "unknown")
        args = tool_call.get("args", {}) or {}

        hit = self._pre_hook(tool_name, args)
        if hit is not None:
            return ToolMessage(
                content=self._blocked_message(hit),
                tool_call_id=tool_call.get("id", ""),
                name=tool_name,
            )

        started = time.perf_counter()
        try:
            result = await handler(request)
        except Exception as exc:
            self._log.write(
                session_id=self._session_id, user=self._user, tool_name=tool_name,
                params_digest=_digest(args, DIGEST_LIMIT), result_status="failed",
                block_reason=f"{type(exc).__name__}: {exc}"[:200],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        self._post_hook(tool_name, args, result, int((time.perf_counter() - started) * 1000))
        return result
