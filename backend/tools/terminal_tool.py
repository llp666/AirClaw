"""terminal —— 沙箱终端。

PRD 第二章要求 `terminal` 直接使用 `langchain_community.tools.ShellTool`。实际不可行：
ShellTool 的字段只有 process/name/description/args_schema/ask_human_input，**没有 root_dir
参数**（PRD 描述的「用 root_dir 沙箱化」不存在），其 _run 只是把命令交给宿主机上的持久
bash 进程，既无黑名单也无超时，更无法进入容器。

本实现改为自定义 BaseTool，_run 内经由 SandboxRunner 进入无网络容器执行，
以满足 PRD 第八章「宿主机不得直接运行 Agent 生成的任何指令」。
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from tools.artifact_paths import PathNotWritable, resolve_writable
from tools.sandbox import SandboxRunner

# 高危指令黑名单。与 rules/secrecy_rules.json 中的 destructive_command_v1 互为补充：
# 规则库面向「审查」由保密管理员维护，本列表面向「误操作防护」由开发维护。
_BLACKLIST: list[tuple[str, str]] = [
    (r"rm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*f|rm\s+-[a-zA-Z]*f[a-zA-Z]*[rR]", "递归强制删除"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启"),
    (r"\bdd\s+if=", "裸设备写入"),
    (r">\s*/dev/[sh]d", "写入块设备"),
    (r":\s*\(\s*\)\s*\{.*\}\s*;\s*:", "fork 炸弹"),
    (r"\bchmod\s+-R\s+777\b", "全权限递归改写"),
    (r"\bmount\b|\bumount\b", "挂载操作"),
    (r"/proc/|/sys/", "访问内核接口"),
    (r"\bdocker\b", "容器嵌套"),
]

_BLACKLIST_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in _BLACKLIST]


class TerminalInput(BaseModel):
    command: str = Field(
        description="要在沙箱容器内执行的 shell 命令，例如 `python -m pytest tests/ -v`"
    )
    out_dir: str | None = Field(
        default=None,
        description="需要保留产物时，指定一个项目内目录（backend/memory/、backend/reports/ "
        "或 backend/tests/ 之下），它会被挂载为容器内的 /workspace/out，且执行结束后"
        "**保留**。留空则 /workspace/out 是一次性目录，执行结束即销毁，产物不保留。",
    )


class TerminalTool(BaseTool):
    name: str = "terminal"
    description: str = (
        "在无网络的隔离沙箱容器内执行 shell 命令。\n"
        "环境约定：\n"
        "  - 项目目录以**只读**方式挂载在 /workspace/src，可读取不可修改\n"
        "  - 工作目录为 /workspace（只读），每次调用创建全新容器，执行结束即销毁\n"
        "  - 容器无网络，无法访问任何外部服务；单次执行有超时与 CPU/内存/进程数限制\n"
        "输出与产物：\n"
        "  - 命令的 stdout/stderr 会截断后返回给你\n"
        "  - 要产出**文件**（测试日志、覆盖率数据等）时，写入 /workspace/out\n"
        "  - 默认情况下 /workspace/out 是一次性目录，命令结束即销毁，产物随之丢失。\n"
        "    需要保留时必须传入 out_dir（backend/memory/、backend/reports/ 或\n"
        "    backend/tests/ 下的路径），该目录会被挂载为 /workspace/out 并在执行后保留。\n"
        "适用场景：运行测试、调用命令行工具、查看文件、执行脚本。"
    )
    args_schema: type[BaseModel] = TerminalInput

    _runner: SandboxRunner = PrivateAttr()

    #: 项目根目录，用于校验 out_dir 是否落在可写白名单内
    root_dir: Path

    def __init__(self, runner: SandboxRunner, root_dir: Path, **kwargs) -> None:
        super().__init__(root_dir=Path(root_dir), **kwargs)
        self._runner = runner

    def _run(self, command: str, out_dir: str | None = None, run_manager=None) -> str:
        for pattern, why in _BLACKLIST_RE:
            if pattern.search(command):
                return (
                    f"[已拦截] 命令命中高危指令黑名单（{why}），未执行。\n"
                    f"命中的模式：{pattern.pattern}\n"
                    f"如确需执行，请联系管理员通过受控流程处理。"
                )

        # out_dir 由模型给出，必须走与 write_file 相同的白名单与遍历防护，
        # 否则沙箱就获得了向项目任意位置写文件的能力。
        keep_dir: Path | None = None
        if out_dir:
            try:
                keep_dir = resolve_writable(out_dir, self.root_dir)
            except PathNotWritable as exc:
                # 前缀用 [已拦截]，让 post_hook 把这次调用记为 blocked
                return f"[已拦截] out_dir 不可用：{exc}"

        result = self._runner.run(["bash", "-lc", command], out_dir=keep_dir)
        return result.render()
