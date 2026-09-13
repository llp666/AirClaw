"""read_file —— 项目内文件读取。

PRD 第二章要求直接使用 `langchain_community.tools.file_management.ReadFileTool`。
**这一条可以照做**：ReadFileTool 继承 BaseFileToolMixin，有真正的 root_dir，
内部通过 get_relative_path() 做路径遍历检查（`..` 逃逸会被拒绝），是纯 Python 文件读取。

与 terminal / python_repl 不同，它不涉及任何执行动作，无需进沙箱。
本模块只做一层薄封装，补上 README 要求的输出截断（10,000 字符）。
"""

from __future__ import annotations

from pathlib import Path

from langchain_community.tools.file_management import ReadFileTool

# README 规定的输出截断上限
READ_LIMIT = 10_000

#: 二进制文档格式。read_file 只能读纯文本，碰上这些直接给出改走检索的指引，
#: 而不是把 "utf-8 codec can't decode byte 0xf2 ..." 这种无信息量的报错丢给模型——
#: 实测模型收到它之后会接着去试 python_repl / terminal 打开原文件，连试数轮。
BINARY_SUFFIXES = {".docx", ".doc", ".pdf", ".xlsx", ".xls"}

_BINARY_GUIDE = (
    "[读不了] {path} 是二进制文档（{suffix}），read_file 只能读纯文本文件。\n"
    "要了解已入库文档的内容，请改用 search_knowledge_base 检索"
    "（知识库语料在 backend/knowledge/ 下）；想看到更多内容就换关键词再检索一次，"
    "打开原文件这条路走不通。"
)


class ReadFileCapTool(ReadFileTool):
    """与 ReadFileTool 行为一致，仅增加输出截断与二进制文档的改道提示。

    工具名沿用 LangChain 默认的 "read_file"，与 PRD 要求的名称一致。
    """

    def _run(self, file_path: str, run_manager=None) -> str:
        suffix = Path(file_path).suffix.lower()
        if suffix in BINARY_SUFFIXES:
            return _BINARY_GUIDE.format(path=file_path, suffix=suffix)

        content = super()._run(file_path, run_manager)
        if len(content) <= READ_LIMIT:
            return content
        return (
            content[:READ_LIMIT]
            + f"\n...[truncated，原始长度 {len(content)} 字符]"
        )


def build_read_file_tool(root_dir: Path) -> ReadFileCapTool:
    """构造 read_file 工具，root_dir 固定为项目根目录。"""
    return ReadFileCapTool(root_dir=str(root_dir))
