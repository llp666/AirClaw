"""知识库语料管理接口（前端的上传入口）。

    POST   /api/knowledge/upload        上传语料到**待审目录**（不入库）
    GET    /api/knowledge/pending       列出待审语料
    POST   /api/knowledge/publish       确认入库（待审 → knowledge/，并重建索引）
    DELETE /api/knowledge/pending       退回（丢弃待审文件）
    GET    /api/knowledge/files         列出已入库语料
    DELETE /api/knowledge/files         删除已入库语料
    POST   /api/knowledge/rebuild       强制重建索引

## 为什么是两步

PRD 第二章规定：「知识库语料由管理员从内网文档源离线导入至 knowledge/ 目录，
导入前须经保密审查确认可入库存放。」

早期实现是在线上传即入库存放，跳过了人工审查环节——传一个文件就立刻可被检索，
等于把保密审查这一关开在了流程之外。现在拆成两步：

    上传 → backend/knowledge_pending/   （不参与索引，检索不到）
    确认 → backend/knowledge/           （入库，索引立即重建）

待审目录刻意放在 knowledge/ **之外**：知识库索引递归扫描 knowledge/ 下的所有文件，
待审文件若放在里面，就得在索引逻辑里再加一层排除规则；分开放之后，
「knowledge/ 里的就是已入库的」这条不变式天然成立，索引侧无需任何改动。

## 安全约束

  后缀白名单  .md .txt .pdf .docx .xlsx .csv
  大小上限    单文件 50MB，**边读边计数**，不信任 Content-Length
  文件名清洗  剥离路径成分与控制字符，拒绝 . 与 ..
  上传、入库、退回、删除全部写入审计日志（tool_name=knowledge_api）
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from config import BACKEND_DIR, get_settings
from graph.agent import agent_manager
from graph.audit_hooks import AuditLog

logger = logging.getLogger("airclaw.api.knowledge")

router = APIRouter()

# 与 tools/knowledge_index.py 的 SUPPORTED_SUFFIXES 保持一致。
# 表格（.xlsx/.csv）单独列出：它们的切块方式与文档不同（按行分组、重复列名），
# 见 knowledge_index.load_table_documents。
ALLOWED_SUFFIXES = {".md", ".txt", ".pdf", ".docx", ".xlsx", ".csv"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
READ_CHUNK = 1024 * 1024

# 文件名中不允许出现的字符（路径分隔符、控制字符、Windows 保留字符）
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _audit(action: str, detail: str, status: str = "executed", reason: str | None = None) -> None:
    try:
        AuditLog(BACKEND_DIR / "audit").write(
            session_id="knowledge_api",
            user=get_settings().app_user,
            tool_name="knowledge_api",
            params_digest=f"{action}: {detail}"[:200],
            result_status=status,
            block_reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — 审计失败不应导致接口 500
        logger.error("写入知识库审计日志失败：%s", exc)


def _safe_name(raw: str) -> str:
    """把上传的文件名清洗为安全的裸文件名。"""
    # 先按两种分隔符取最后一段，再剔除非法字符
    name = raw.replace("\\", "/").split("/")[-1]
    name = _UNSAFE_RE.sub("_", name).strip().strip(".")

    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail=f"文件名不合法：{raw!r}")

    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型 {suffix or '(无扩展名)'}，"
            f"仅接受 {'、'.join(sorted(ALLOWED_SUFFIXES))}",
        )
    return name


def _dir(which: str) -> Path:
    """取待审目录或已入库目录，顺带确保存在。"""
    settings = get_settings()
    path = (
        settings.knowledge_pending_path if which == "pending" else settings.knowledge_path
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _list(directory: Path) -> list[dict]:
    return [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(directory.iterdir())
        if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES
    ]


# --------------------------------------------------------------------------
# 上传（落到待审目录，不入库）
# --------------------------------------------------------------------------


@router.post("/knowledge/upload")
async def upload(file: UploadFile = File(description="语料文件")) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    name = _safe_name(file.filename)
    target = _dir("pending") / name
    existed = target.is_file()

    # 边写边计数：Content-Length 可被伪造，不能作为限额依据
    written = 0
    try:
        with target.open("wb") as fh:
            while chunk := await file.read(READ_CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    fh.close()
                    target.unlink(missing_ok=True)
                    _audit("upload", name, "blocked", f"超过大小上限 {MAX_UPLOAD_BYTES}")
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        _audit("upload", name, "failed", f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"写入失败：{exc}") from exc
    finally:
        await file.close()

    _audit("upload", f"{name} ({written} 字节) → 待审")
    logger.info("语料已上传至待审目录：%s（%d 字节）", name, written)

    return {
        "name": name,
        "size": written,
        "replaced": existed,
        "pending": True,
        "message": "已上传至待审目录，**尚未入库、检索不到**。"
        "请由保密管理员确认内容可入库存放后，再执行「确认入库」。",
    }


# --------------------------------------------------------------------------
# 待审
# --------------------------------------------------------------------------


@router.get("/knowledge/pending")
async def list_pending() -> dict:
    files = _list(_dir("pending"))
    return {"files": files, "count": len(files)}


@router.post("/knowledge/publish")
async def publish(name: str = Query(description="待审目录下的文件名")) -> dict:
    """确认入库：待审目录 → knowledge/，并重建索引使其立即可检索。"""
    safe = _safe_name(name)
    source = _dir("pending") / safe
    if not source.is_file():
        raise HTTPException(status_code=404, detail=f"待审目录中没有该文件：{safe}")

    target = _dir("published") / safe
    replaced = target.is_file()
    shutil.move(str(source), str(target))

    try:
        agent_manager.rebuild_knowledge_index(force=True)
        indexed = True
    except Exception as exc:  # noqa: BLE001 — 入库已成事实，索引失败如实返回
        logger.warning("语料已入库但索引重建失败：%s", exc)
        indexed = False

    _audit("publish", f"{safe}（{'覆盖' if replaced else '新增'}，索引重建={indexed}）")
    logger.info("语料已确认入库：%s", safe)
    return {"name": safe, "published": True, "replaced": replaced, "index_rebuilt": indexed}


@router.delete("/knowledge/pending")
async def discard(name: str = Query(description="待审目录下的文件名")) -> dict:
    """退回：丢弃待审文件，不入库。"""
    safe = _safe_name(name)
    target = _dir("pending") / safe
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"待审目录中没有该文件：{safe}")

    target.unlink()
    _audit("discard", safe)
    logger.info("待审语料已退回丢弃：%s", safe)
    return {"name": safe, "discarded": True}


# --------------------------------------------------------------------------
# 已入库
# --------------------------------------------------------------------------


@router.get("/knowledge/files")
async def list_files() -> dict:
    directory = _dir("published")
    files = _list(directory)

    settings = get_settings()
    indexed = None
    manifest_file = settings.storage_path / "knowledge_index" / "manifest.json"
    if manifest_file.is_file():
        try:
            m = json.loads(manifest_file.read_text(encoding="utf-8"))
            indexed = {"files": m.get("count"), "chunks": m.get("chunks"),
                       "vector_ok": m.get("vector_ok")}
        except Exception:  # noqa: BLE001
            indexed = None

    return {"files": files, "count": len(files), "index": indexed}


@router.delete("/knowledge/files")
async def delete_file(path: str = Query(description="knowledge/ 下的文件名")) -> dict:
    name = _safe_name(path)
    target = _dir("published") / name

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{name}")

    target.unlink()
    _audit("delete", name)
    logger.info("知识库语料已删除：%s", name)
    return {"name": name, "deleted": True}


@router.post("/knowledge/rebuild")
async def rebuild_index(force: bool = Query(default=True)) -> dict:
    """重建知识库索引。"""
    try:
        result = agent_manager.rebuild_knowledge_index(force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"索引重建失败：{exc}") from exc
    _audit("rebuild", str(result))
    return result
