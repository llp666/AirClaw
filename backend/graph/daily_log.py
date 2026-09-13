"""每日工作日志 —— 由后端在每轮对话结束后自动追加。

对应 AGENTS.md 记忆协议的分工：长期记忆记「结论」，每日日志记「过程」。

日志原本由 Agent 自己写，但实测四轮里只写了两轮——协议写得再明确，模型仍不稳定
遵守。而一个一半概率缺失的日志比没有更糟：人看到当天没有日志，会得出「这天没做事」
的错误结论，会话 JSON 又太原始、不适合人直接翻。故改由后端在落盘阶段机械追加。

只追加，不重写：每次以 "a" 模式打开文件追加一段，绝不读改写，避免与他轮次竞争。
Agent 的 write_file 仍可写这个目录，但会经过丢行校验（见 tools/write_file_tool.py），
不会覆盖掉这里写入的内容。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("airclaw.daily_log")

# 与提示词、审计日志保持同一时区口径
CST = timezone(timedelta(hours=8))

SUBJECT_LIMIT = 40
TITLE_LIMIT = 30
REPLY_LIMIT = 120


def _clip(text: str, limit: int) -> str:
    """压平空白并截断——日志是一行一条，换行会破坏 Markdown 结构。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def append_turn(
    log_dir: Path,
    *,
    session_id: str,
    session_title: str,
    user_message: str,
    reply: str,
    artifacts: list[str],
    now: datetime | None = None,
) -> Path | None:
    """追加一轮对话的记录。返回日志路径，写失败返回 None。

    日志写不进去不该让整轮对话失败，故只记 warning 不上抛。
    """
    moment = now or datetime.now(CST)
    path = log_dir / f"{moment:%Y-%m-%d}.md"

    lines = [f"\n## {moment:%H:%M}  {_clip(user_message, SUBJECT_LIMIT)}"]
    if session_title:
        lines.append(f"- 会话：`{session_id}`「{_clip(session_title, TITLE_LIMIT)}」")
    else:
        lines.append(f"- 会话：`{session_id}`")
    lines.append(
        "- 产出物：" + ("、".join(f"`{a}`" for a in artifacts) if artifacts else "无")
    )
    if reply:
        lines.append(f"- 摘要：{_clip(reply, REPLY_LIMIT)}")

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        with path.open("a", encoding="utf-8") as fh:
            if fresh:
                fh.write(
                    f"# {moment:%Y-%m-%d} 工作日志\n\n"
                    "> 由后端在每轮对话结束时自动追加，只增不改。\n"
                )
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        logger.warning("写入每日日志失败（不影响本轮对话）：%s", exc)
        return None

    return path
