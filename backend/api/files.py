"""文件读写接口（供前端 Monaco 编辑器使用）。

    GET  /api/files?path=memory/MEMORY.md   读取文件内容
    POST /api/files                         保存文件

技能相关接口（列出 / 上传 / 卸载）见 api/skills.py。

## 路径白名单

对应 PRD 第五章第 2 节与 README「files.py — 文件操作」：

    允许的目录前缀   workspace/  memory/  skills/  knowledge/  reports/
    允许的根目录文件 SKILLS_SNAPSHOT.md

路径先做规范化再做前缀比对，`..` 逃逸、绝对路径、盘符路径一律拒绝，
并**写入审计日志**（越权尝试是要留痕的安全事件，不能静默丢弃）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from config import BACKEND_DIR, get_settings
from graph.audit_hooks import AuditLog
from graph.agent import agent_manager

logger = logging.getLogger("airclaw.api.files")

router = APIRouter()

# 允许读写的目录前缀（相对 backend/）
ALLOWED_PREFIXES = ("workspace/", "memory/", "skills/", "knowledge/", "reports/")
# 允许读写的根目录文件
ALLOWED_ROOT_FILES = ("SKILLS_SNAPSHOT.md",)

# 单文件读写上限，防止误操作把超大文件塞进编辑器
MAX_FILE_BYTES = 2 * 1024 * 1024


class SaveFileRequest(BaseModel):
    path: str = Field(description="相对 backend/ 的路径")
    content: str = Field(description="文件内容")


def _audit_denied(path: str, reason: str) -> None:
    try:
        AuditLog(BACKEND_DIR / "audit").write(
            session_id="file_api",
            user=get_settings().app_user,
            tool_name="files_api",
            params_digest=path[:200],
            result_status="blocked",
            block_reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — 审计失败不应导致接口 500
        logger.error("写入越权审计日志失败：%s", exc)


def _resolve(raw_path: str) -> Path:
    """把前端传来的路径解析为受控目录内的绝对路径，越权即拒绝。"""
    if not raw_path or not raw_path.strip():
        raise HTTPException(status_code=400, detail="path 不能为空")

    candidate = raw_path.strip().replace("\\", "/")

    # 绝对路径与盘符路径直接拒绝（Windows 上 Path 会按盘符处理，容易绕过前缀检查）
    if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
        _audit_denied(raw_path, "绝对路径不允许")
        raise HTTPException(status_code=403, detail="仅允许相对 backend/ 的路径")

    resolved = (BACKEND_DIR / candidate).resolve()
    root = BACKEND_DIR.resolve()

    # 规范化后再验证仍在 backend/ 之内，拦截 ../ 逃逸
    if not resolved.is_relative_to(root):
        _audit_denied(raw_path, "路径逃逸（越出 backend 目录）")
        raise HTTPException(status_code=403, detail="越权路径")

    rel = resolved.relative_to(root).as_posix()

    if rel in ALLOWED_ROOT_FILES:
        return resolved
    if any(rel.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return resolved

    _audit_denied(raw_path, f"路径不在白名单内：{rel}")
    raise HTTPException(
        status_code=403,
        detail=f"路径不在白名单内。允许的目录：{', '.join(ALLOWED_PREFIXES)}",
    )


@router.get("/files")
async def read_file(path: str = Query(description="相对 backend/ 的路径")) -> dict:
    target = _resolve(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{path}")

    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{size} 字节，上限 {MAX_FILE_BYTES}）",
        )

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="文件不是 UTF-8 文本，无法编辑")

    return {
        "path": target.relative_to(BACKEND_DIR.resolve()).as_posix(),
        "content": content,
        "size": size,
    }


@router.post("/files")
async def save_file(request: SaveFileRequest) -> dict:
    target = _resolve(request.path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existed = target.is_file()
    target.write_text(request.content, encoding="utf-8")

    rel = target.relative_to(BACKEND_DIR.resolve()).as_posix()
    logger.info("文件已保存：%s（%d 字符）", rel, len(request.content))

    # MEMORY.md 变更后重建检索索引，让编辑立即生效
    memory_index_rebuilt = False
    if rel == "memory/MEMORY.md":
        try:
            agent_manager.rebuild_memory_index(force=True)
            memory_index_rebuilt = True
            logger.info("MEMORY.md 已保存，记忆索引已重建")
        except Exception as exc:  # noqa: BLE001 — 索引失败不影响文件已保存这一事实
            logger.warning("MEMORY.md 已保存，但记忆索引重建失败：%s", exc)

    return {
        "path": rel,
        "saved": True,
        "created": not existed,
        "size": len(request.content.encode("utf-8")),
        "memory_index_rebuilt": memory_index_rebuilt,
    }
