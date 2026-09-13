"""会话持久化。

以 JSON 文件管理每个会话的完整历史，路径 sessions/{session_id}.json。

文件格式（v2）：

    {
      "title": "生成单元测试报告",
      "created_at": 1706000000.0,
      "updated_at": 1706000100.0,
      "compressed_context": "用户之前请求为 shell_tool.py 生成测试...",
      "messages": [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "...", "tool_calls": [...]},
        {"role": "assistant", "content": "..."}
      ]
    }

v1 兼容：早期文件可能是纯数组 `[...]`，_read() 会自动迁移为 v2。

一次工具调用会产生**多条连续的 assistant 消息**（工具调用前的文本段、工具执行后的
文本段）。LLM 要求严格的 user/assistant 交替，因此 load_for_agent() 会把连续的
assistant 消息合并为一条供模型使用，而磁盘上仍如实保留分段结构（Raw Messages 可查）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

logger = logging.getLogger("airclaw.session")

SUMMARY_PREFIX = "[以下是之前对话的摘要]\n"


class SessionManager:
    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir
        self._archive_dir = sessions_dir / "archive"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    # ---- 路径 ----

    def _path(self, session_id: str) -> Path:
        # 只取文件名部分，杜绝 session_id 中夹带路径导致越权读写
        safe = Path(session_id).name
        if not safe or safe in {".", ".."}:
            raise ValueError(f"非法会话 ID：{session_id!r}")
        return self._dir / f"{safe}.json"

    # ---- 读写 ----

    def _read(self, session_id: str) -> dict:
        path = self._path(session_id)
        if not path.is_file():
            return {"title": "新会话", "created_at": time.time(),
                    "updated_at": time.time(), "messages": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("会话文件损坏，按空会话处理：%s（%s）", path.name, exc)
            return {"title": "新会话", "created_at": time.time(),
                    "updated_at": time.time(), "messages": []}

        # v1 -> v2 迁移：早期文件是纯数组
        if isinstance(data, list):
            return {"title": "新会话", "created_at": time.time(),
                    "updated_at": time.time(), "messages": data}
        data.setdefault("messages", [])
        return data

    def _write(self, session_id: str, data: dict) -> None:
        data["updated_at"] = time.time()
        self._path(session_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---- 对外接口 ----

    def create(self, title: str = "新会话") -> dict:
        session_id = uuid.uuid4().hex[:12]
        now = time.time()
        data = {"title": title, "created_at": now, "updated_at": now, "messages": []}
        self._write(session_id, data)
        return {"id": session_id, **data}

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).is_file()

    def list_sessions(self) -> list[dict]:
        """列出全部会话，按更新时间倒序。"""
        out: list[dict] = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, list):  # v1 文件
                data = {"title": "新会话", "messages": data}
            out.append(
                {
                    "id": path.stem,
                    "title": data.get("title", "新会话"),
                    "created_at": data.get("created_at", 0),
                    "updated_at": data.get("updated_at", 0),
                    "message_count": len(data.get("messages", [])),
                }
            )
        return sorted(out, key=lambda s: s["updated_at"], reverse=True)

    def rename(self, session_id: str, title: str) -> None:
        data = self._read(session_id)
        data["title"] = title
        self._write(session_id, data)

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path.is_file():
            path.unlink()
            return True
        return False

    def load(self, session_id: str) -> dict:
        """返回原始会话数据。"""
        return self._read(session_id)

    def load_messages(self, session_id: str) -> list[dict]:
        """返回原始消息数组（保留分段的 assistant 消息，供 Raw Messages 查看）。"""
        return self._read(session_id).get("messages", [])

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
    ) -> None:
        """追加一条消息到会话文件。"""
        data = self._read(session_id)
        message: dict = {"role": role, "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        message["ts"] = time.time()
        data["messages"].append(message)
        self._write(session_id, data)

    def get_compressed_context(self, session_id: str) -> str:
        return self._read(session_id).get("compressed_context", "")

    def load_for_agent(self, session_id: str) -> list[dict]:
        """为 LLM 优化过的历史：合并连续 assistant 消息、注入压缩摘要。

        与 load_messages() 的区别：LLM 要求严格交替，磁盘上却可能有连续多条
        assistant 消息（工具调用产生的多段响应），此处将它们合并为一条。
        """
        data = self._read(session_id)
        merged: list[dict] = []

        for msg in data.get("messages", []):
            role = msg.get("role")
            content = msg.get("content", "") or ""
            if role == "assistant" and merged and merged[-1]["role"] == "assistant":
                # 合并到上一条 assistant 消息
                prev = merged[-1]
                if content:
                    prev["content"] = f"{prev['content']}\n\n{content}".strip()
                continue
            merged.append({"role": role, "content": content})

        # 丢弃末尾悬空的 user 消息（上一次请求中途失败留下的），
        # 否则会让模型看到连续两条 user 消息
        while merged and merged[-1]["role"] != "assistant":
            merged.pop()

        summary = data.get("compressed_context", "")
        if summary:
            merged.insert(
                0,
                {"role": "assistant", "content": SUMMARY_PREFIX + summary},
            )
        return merged

    def compress_history(self, session_id: str, summary: str, n: int) -> dict:
        """把前 n 条消息归档到 sessions/archive/，并从会话中移除。

        摘要写入 compressed_context；多次压缩以 --- 分隔累积。
        """
        data = self._read(session_id)
        messages = data.get("messages", [])
        n = max(0, min(n, len(messages)))
        archived, remaining = messages[:n], messages[n:]

        if archived:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            archive_path = self._archive_dir / f"{session_id}_{stamp}.json"
            archive_path.write_text(
                json.dumps(archived, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        existing = data.get("compressed_context", "")
        data["compressed_context"] = (
            f"{existing}\n---\n{summary}" if existing else summary
        )
        data["messages"] = remaining
        self._write(session_id, data)

        return {"archived_count": len(archived), "remaining_count": len(remaining)}
