"""python_repl —— 沙箱 Python 解释器。

PRD 第二章要求直接使用 `langchain_experimental.tools.PythonREPLTool`。实际不可行：
其底层 `PythonREPL.run()` 用 `exec()` 在**宿主 Python 进程内**执行代码，Agent 生成的
代码可直接读写宿主机任意文件、发起网络请求；该类的源码里自带 `warn_once()`，注释原文为
"Warn against dangers of PythonREPL"。

本实现改为自定义 BaseTool：代码写入输出目录后，在无网络容器内以独立进程执行，
满足 PRD 第八章的隔离要求。
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from config import get_settings
from tools.sandbox import SandboxRunner

# 容器内执行入口的路径（位于可写的输出目录内）
SCRIPT_NAME = "_airclaw_repl.py"


class PythonReplInput(BaseModel):
    code: str = Field(
        description="要执行的完整 Python 代码。需要看到结果请用 print 输出；"
        "代码在独立进程内执行，无法访问上一次调用的变量。"
    )


class PythonReplTool(BaseTool):
    name: str = "python_repl"
    description: str = (
        "在无网络的隔离沙箱容器内执行 Python 代码。\n"
        "环境约定：\n"
        "  - 项目目录以**只读**方式挂载在 /workspace/src\n"
        "  - 当前工作目录为 /workspace/out（可写），但该目录**执行结束即销毁**，"
        "写入其中的任何文件都会丢失。需要保留的产物请改用 `write_file` 落盘。\n"
        "  - 容器无网络，无法访问任何外部服务；无宿主机凭证\n"
        "  - 代码在独立进程中执行，**不保留任何变量或状态**，每次调用都是全新解释器\n"
        "  - 需要看到结果必须用 print 输出，仅写表达式不会回显\n"
        "适用场景：逻辑计算、数据处理、脚本执行、结果汇总与格式化。"
    )
    args_schema: type[BaseModel] = PythonReplInput

    _runner: SandboxRunner = PrivateAttr()

    def __init__(self, runner: SandboxRunner, **kwargs) -> None:
        super().__init__(**kwargs)
        self._runner = runner

    def _run(self, code: str, run_manager=None) -> str:
        # 临时目录必须位于项目内（见 settings.sandbox_tmp_path 的说明），
        # 否则 Docker Desktop 会因目录未共享而拒绝创建容器。
        tmp_root = get_settings().sandbox_tmp_path
        tmp_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix=f"repl-{uuid.uuid4().hex[:8]}-", dir=tmp_root
        ) as tmp:
            out_dir = Path(tmp)
            (out_dir / SCRIPT_NAME).write_text(code, encoding="utf-8")

            # 容器内路径由挂载模式决定（绑定模式是固定路径，命名卷模式需换算），
            # 直接拼 /workspace/out 在命名卷模式下会找不到脚本。
            container_out = self._runner.container_out_path(out_dir)

            result = self._runner.run(
                ["python", f"{container_out}/{SCRIPT_NAME}"],
                out_dir=out_dir,
                # 工作目录设为可写的输出目录，让相对路径写入直接可用
                workdir=container_out,
            )
        return result.render()
