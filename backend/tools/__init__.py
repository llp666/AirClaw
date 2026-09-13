"""AirClaw 核心工具层。

内置工具（按 PRD 第二章，外加一个写入通道）：

    terminal_tool.py             terminal              沙箱终端（自定义 BaseTool）
    python_repl_tool.py          python_repl           沙箱 Python 解释器（自定义 BaseTool）
    read_file_tool.py            read_file             项目内文件读取（封装 LangChain ReadFileTool）
    search_knowledge_tool.py     search_knowledge_base 知识库混合检索
    write_file_tool.py           write_file            受控目录写入（PRD 第二章之外的扩展）

    skills_scanner.py            非工具：启动时扫描 skills/**/SKILL.md 生成 SKILLS_SNAPSHOT.md
    artifact_paths.py            非工具：Agent 可写路径的白名单与遍历防护
    sandbox.py                   非工具：Docker 沙箱执行器，terminal/python_repl 的唯一执行入口

关于 PRD 第二章「直接使用 LangChain 内置工具」的说明：

    read_file  —— 照做。ReadFileTool 有真正的 root_dir 与路径遍历防护，安全合规。
    terminal   —— 未照做。ShellTool 没有 root_dir 参数（PRD 描述的沙箱化不存在），
                  且在宿主机进程内执行，无法进入容器，与 PRD 第八章冲突。改为自定义实现。
    python_repl—— 未照做。PythonREPLTool 用 exec() 在宿主进程内执行，源码自带危险警告，
                  同样与 PRD 第八章冲突。改为自定义实现。
    write_file —— 第二章未列。PRD 通篇假定 Agent 能落盘（AGENTS.md 的记忆协议、
                  code_test 第二与第六步），但清单里没有写工具，而 terminal/python_repl
                  的沙箱输出目录是一次性的。补上缺失的一环，白名单见 artifact_paths.py。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool

from config import BACKEND_DIR, get_settings
from tools.knowledge_index import KnowledgeIndex
from tools.python_repl_tool import PythonReplTool
from tools.read_file_tool import build_read_file_tool
from tools.sandbox import SandboxError, SandboxResult, SandboxRunner
from tools.search_knowledge_tool import SearchKnowledgeTool
from tools.skills_scanner import scan_skills
from tools.terminal_tool import TerminalTool
from tools.write_file_tool import WriteFileTool, build_write_file_tool

__all__ = [
    "KnowledgeIndex",
    "SandboxError",
    "SandboxResult",
    "SandboxRunner",
    "SearchKnowledgeTool",
    "TerminalTool",
    "PythonReplTool",
    "WriteFileTool",
    "build_read_file_tool",
    "build_write_file_tool",
    "get_all_tools",
    "scan_skills",
]


def get_all_tools(
    base_dir: Path | None = None, *, knowledge_index: KnowledgeIndex | None = None
) -> list[BaseTool]:
    """构造并返回全部内置工具。base_dir 默认为项目根目录。

    knowledge_index 可外部注入，便于调用方（如 AgentManager）持有同一实例，
    以便在语料变更后主动重建索引。
    """
    settings = get_settings()
    root = Path(base_dir) if base_dir else BACKEND_DIR.parent
    runner = SandboxRunner(settings)

    tools: list[BaseTool] = [
        TerminalTool(runner, root),
        PythonReplTool(runner),
        build_read_file_tool(root),
        build_write_file_tool(root),
        SearchKnowledgeTool(knowledge_index or KnowledgeIndex(settings)),
    ]

    # 提前校验沙箱可用性，让部署问题在启动时就暴露，
    # 而不是等用户发出第一条含工具调用的消息才失败。
    runner.ensure_image()

    return tools
