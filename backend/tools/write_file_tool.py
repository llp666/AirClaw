"""write_file —— 写入项目内的受控目录。

对应 PRD 第四章「文件即记忆」与第三章 code_test 技能的归档步骤：Agent 必须能把
一条约定写进 memory/MEMORY.md，把测试报告写进 reports/。

**这是对 PRD 第二章「4 个核心工具」的扩展。** PRD 通篇假定 Agent 能落盘——
AGENTS.md 的记忆协议写着「再通过文件写入工具落盘」，code_test 第二、六步要求
生成 tests/ 并归档到 reports/——但第二章的清单里没有任何写工具，而 terminal 与
python_repl 都在沙箱内执行：项目目录只读挂载，输出目录执行完即销毁。二者矛盾，
此处补上缺失的一环。白名单与取舍理由见 tools/artifact_paths.py。
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from tools.artifact_paths import MAX_WRITE_BYTES, PathNotWritable, resolve_writable

#: 长期记忆文件（相对项目根目录）。它是唯一要求条目去重的文件
MEMORY_REL = "backend/memory/MEMORY.md"

#: 每日日志目录。这类文件只追加，不允许丢弃既有内容
LOG_DIR_REL = "backend/memory/logs/"

# 被拒绝时统一用这个前缀：graph/audit_hooks.py 的 post_hook 据此把本次调用记为
# blocked 而非 executed。否则一次被拒的越权写入在审计日志里会显示成执行成功。
DENIED = "[已拦截]"

# 条目标题：`### YYYY-MM-DD 主题`，日期可省。`####` 不会被匹配（### 后必须是空白）
_TOPIC_RE = re.compile(r"^###[ \t]+(?:\d{4}-\d{2}-\d{2}[ \t]+)?(?P<topic>\S.*?)[ \t]*$", re.MULTILINE)


def duplicate_topics(text: str) -> list[str]:
    """找出长期记忆里重复出现的主体。

    主题取自 `### YYYY-MM-DD 主题` 的日期之后部分——同一件事换了日期仍是同一主题，
    正是 AGENTS.md 要求"就地更新而非新增"的情形。
    """
    seen: dict[str, int] = {}
    for match in _TOPIC_RE.finditer(text):
        topic = match.group("topic").strip()
        seen[topic] = seen.get(topic, 0) + 1
    return [topic for topic, n in seen.items() if n > 1]


def dropped_lines(existing: str, new: str) -> list[str]:
    """既有内容中在 new 里找不到的非空行——即本次写入会丢掉的部分。

    每日日志是只追加的：模型必须先读全文再追加写回。它偶尔会漏掉读这一步，
    而 write_file 是覆盖式写入，漏读就意味着当天已记的内容被静默抹掉。
    """
    new_lines = {line.strip() for line in new.splitlines()}
    return [
        line for line in existing.splitlines()
        if line.strip() and line.strip() not in new_lines
    ]


class WriteFileInput(BaseModel):
    file_path: str = Field(
        description="相对项目根目录的路径，必须位于 backend/memory/、backend/reports/ "
        "或 backend/tests/ 之下。例如 backend/memory/MEMORY.md、"
        "backend/reports/shell_tool/2026-09-12/report.md"
    )
    content: str = Field(description="要写入的完整文件内容。覆盖式写入，不是追加。")


class WriteFileTool(BaseTool):
    name: str = "write_file"
    description: str = (
        "把文本写入项目内受控目录。这是唯一能产生持久产物的写入通道——"
        "terminal 与 python_repl 在沙箱容器内执行，其输出目录默认是一次性的。\n"
        "可写目录（路径以项目根目录为基准，写法与 read_file 一致）：\n"
        "  - backend/memory/   长期记忆、每日日志\n"
        "  - backend/reports/  测试报告归档\n"
        "  - backend/tests/    生成的测试用例\n"
        "其余路径一律拒绝，包括 workspace/、skills/、knowledge/ 与项目外的任何路径。\n"
        "**语义为覆盖式写入**：只传增量内容会把原文件截断。"
        "修改已有文件前必须先用 read_file 读取全文，在内存中改好后整体写回，"
        "不要丢失既有内容。\n"
        "backend/memory/logs/ 下的每日日志由系统自动维护，你不应写它；"
        "若确需写入，丢弃任何已有行都会被直接拒绝。\n"
        "父目录不存在时会自动创建。"
    )
    args_schema: type[BaseModel] = WriteFileInput

    #: 项目根目录，所有可写路径以此为基准
    root_dir: Path

    def _run(self, file_path: str, content: str, run_manager=None) -> str:
        try:
            target = resolve_writable(file_path, self.root_dir)
        except PathNotWritable as exc:
            return f"{DENIED} {exc}"

        if target.is_dir():
            return f"{DENIED} 目标是一个目录，无法写入：{file_path}"

        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            return (
                f"{DENIED} 内容 {len(data)} 字节，超过单文件上限 "
                f"{MAX_WRITE_BYTES // 1024 // 1024}MB。请拆分后分次写入。"
            )

        rel = target.relative_to(self.root_dir.resolve()).as_posix()

        # 每日日志由后端自动维护（graph/daily_log.py），Agent 不应写它。
        # 但它仍在可写白名单内，模型偶尔会照着「每日日志」的旧理解去写——覆盖式写入
        # 会静默抹掉系统记下的整日流水，故这里拒写并要求它先读回来。
        if rel.startswith(LOG_DIR_REL) and target.is_file():
            dropped = dropped_lines(target.read_text(encoding="utf-8"), content)
            if dropped:
                preview = "\n".join(f"    {line}" for line in dropped[:5])
                more = f"\n    …另有 {len(dropped) - 5} 行" if len(dropped) > 5 else ""
                return (
                    f"{DENIED} 本次写入会丢弃 {rel} 中已有的 {len(dropped)} 行内容，"
                    "未执行。每日日志只追加、不覆盖。\n"
                    "请先用 read_file 读回该文件，在其末尾追加本轮记录，再整体写回；"
                    "既有内容必须原样保留。\n"
                    f"会被丢弃的行：\n{preview}{more}"
                )

        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.is_file()
        target.write_text(content, encoding="utf-8")

        result = f"[已写入] {rel}（{'覆盖' if existed else '新建'}，{len(data)} 字节）"

        # 写入已完成，校验只做提示不撤回：内容本身没问题，需要的是让模型当轮合并。
        if rel == MEMORY_REL:
            dups = duplicate_topics(content)
            if dups:
                result += (
                    "\n[记忆校验] 下列主题存在多条条目："
                    + "、".join(dups)
                    + "。长期记忆要求同一主题只保留一条。"
                    "请立即 read_file 读回全文，把重复条目合并为一条"
                    "（保留最新事实、更新日期标题），再整体写回。"
                )
        return result


def build_write_file_tool(root_dir: Path) -> WriteFileTool:
    """构造 write_file 工具，root_dir 固定为项目根目录。"""
    return WriteFileTool(root_dir=Path(root_dir))
