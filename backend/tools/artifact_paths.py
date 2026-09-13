"""Agent 可写路径的统一解析。

Agent 有两条产生持久产物的通道，二者共用同一份白名单与遍历防护：

    write_file              直接写入文本文件（记忆、测试报告、测试用例）
    terminal(out_dir=...)   把沙箱的 /workspace/out 映射到项目内的目录，
                            使 pytest 等执行产生的产物不随容器销毁

路径以**项目根目录**为基准，与 read_file 一致（AGENTS.md 里写的是
`backend/config.py` 这种形式）。白名单只覆盖 Agent 应当产出内容的目录：

    backend/memory/   长期记忆与每日日志（PRD 第四章）
    backend/reports/  code_test 技能的报告归档（PRD 第三章）
    backend/tests/    code_test 技能生成的测试用例

**不含** backend/workspace/（System Prompt 组件，改它等于改 Agent 自身设定）、
backend/skills/（技能说明书，改它等于改 Agent 自身指令）、
backend/knowledge/（语料须经保密审查后由管理员导入，见 PRD 第二章）。
"""

from __future__ import annotations

from pathlib import Path

# 允许 Agent 写入的目录前缀（相对项目根目录）
WRITABLE_PREFIXES = ("backend/memory/", "backend/reports/", "backend/tests/")

# 单文件写入上限，防止一次写入撑爆编辑器与索引
MAX_WRITE_BYTES = 2 * 1024 * 1024


class PathNotWritable(ValueError):
    """路径不在 Agent 可写白名单内。"""


def resolve_writable(raw_path: str, root: Path) -> Path:
    """把 Agent 给出的相对路径解析为白名单目录内的绝对路径。

    越权时抛 PathNotWritable，调用方据此把原因回给模型。
    只做解析与校验，不创建任何东西。
    """
    if not raw_path or not raw_path.strip():
        raise PathNotWritable("路径不能为空")

    candidate = raw_path.strip().replace("\\", "/")

    # 绝对路径与盘符路径直接拒绝：Windows 上 Path 会按盘符处理，容易绕过前缀比对
    if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
        raise PathNotWritable(f"只接受相对项目根目录的路径，收到绝对路径：{raw_path}")

    root = root.resolve()
    resolved = (root / candidate).resolve()

    # 规范化之后再验证仍在项目内，可同时拦截 ../ 逃逸与指向外部的符号链接
    if not resolved.is_relative_to(root):
        raise PathNotWritable(f"路径逃出项目根目录：{raw_path}")

    rel = resolved.relative_to(root).as_posix()
    # 白名单前缀带尾部斜杠，用来拦住 "backend/memory2" 这类同前缀不同目录；
    # 但白名单目录**本身**（如 out_dir="backend/memory"）也必须放行——terminal 的
    # out_dir 传的就是目录，去掉斜杠写是最自然的写法，而工具描述里举的例子正是它。
    if not any(rel == p.rstrip("/") or rel.startswith(p) for p in WRITABLE_PREFIXES):
        raise PathNotWritable(
            f"路径不在可写目录内：{rel}。"
            f"可写目录：{'、'.join(p.rstrip('/') for p in WRITABLE_PREFIXES)}"
        )
    return resolved
