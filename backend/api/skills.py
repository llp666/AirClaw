"""技能管理接口（前端的技能上传入口）。

    GET    /api/skills               列出已安装技能
    POST   /api/skills/upload        上传技能包（zip）
    DELETE /api/skills/{name}        卸载技能

## 技能包格式

技能是一个目录，内含 `SKILL.md`（定义 name / description / 执行步骤）。
上传时打包为 zip，支持两种布局：

    SKILL.md                       ← 单技能，根目录直接放 SKILL.md
    xxx.md
    ─────────────────────────
    my_skill/SKILL.md              ← 推荐：一个顶层目录包住整个技能
    my_skill/scripts/helper.py

技能名取「唯一顶层目录名」，若无唯一顶层目录则取上传文件名的词干。

## 安全约束（zip slip 防护）

zip 条目路径由上传方完全控制，可直接写入 `../../` 实现目录穿越，
或使用绝对路径 / 盘符路径写到任意位置。因此逐条目校验：

  - 拒绝绝对路径、盘符路径、包含 `..` 的路径
  - 拒绝符号链接条目（可指向宿主任意位置）
  - 限制条目总数与解压后总大小（zip 炸弹）
  - 最终路径必须落在 skills/{name}/ 之内（二次校验，防止校验与写入之间被绕过）

解压失败时清理已写入的目录，不留半成品技能。

## 与 PRD 的关系

PRD 第三章将技能定位为「拖入即用」的扩展方式，本接口正是这一能力的落地。
但技能文件最终会进入 System Prompt（通过 SKILLS_SNAPSHOT），其内容会影响
Agent 行为，因此上传后**必须重新生成快照**，且每次上传与卸载都写入审计日志。
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from config import BACKEND_DIR, get_settings
from graph.audit_hooks import AuditLog
from tools.skills_scanner import scan_skills

logger = logging.getLogger("airclaw.api.skills")

router = APIRouter()

MAX_ZIP_BYTES = 20 * 1024 * 1024      # 上传包上限
MAX_TOTAL_BYTES = 50 * 1024 * 1024    # 解压后总大小上限
MAX_ENTRIES = 500                     # 条目数上限


# 技能名只允许字母数字、下划线、连字符与中文，避免路径注入与跨平台问题
_NAME_RE = re.compile(r"^[\w一-鿿-]{1,64}$")


def _audit(action: str, detail: str, status: str = "executed", reason: str | None = None) -> None:
    try:
        AuditLog(BACKEND_DIR / "audit").write(
            session_id="skills_api",
            user=get_settings().app_user,
            tool_name="skills_api",
            params_digest=f"{action}: {detail}"[:200],
            result_status=status,
            block_reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("写入技能审计日志失败：%s", exc)


def _skills_dir() -> Path:
    path = get_settings().skills_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_unsafe_member(name: str) -> str | None:
    """判断 zip 条目是否危险，危险时返回原因。"""
    if not name or name.endswith("/"):
        return None  # 目录条目，交由后续校验

    normalized = name.replace("\\", "/")

    if normalized.startswith("/"):
        return "绝对路径"
    if len(normalized) > 1 and normalized[1] == ":":
        return "盘符路径"
    # 逐段检查，任一段为 .. 即视为穿越
    if any(part == ".." for part in normalized.split("/")):
        return "包含 .. 的路径穿越"
    return None


@router.get("/skills")
async def list_skills() -> dict:
    """列出已安装技能及其 SKILL.md 路径。"""
    skills_dir = _skills_dir()
    out: list[dict] = []

    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            out.append(
                {
                    "name": skill_dir.name,
                    "path": skill_md.relative_to(BACKEND_DIR).as_posix(),
                }
            )
    return {"skills": out}


@router.post("/skills/upload")
async def upload_skill(file: UploadFile = File(description="技能包（zip）")) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    raw = await file.read(MAX_ZIP_BYTES + 1)
    await file.close()

    if len(raw) > MAX_ZIP_BYTES:
        _audit("upload", file.filename, "blocked", "超过上传包上限")
        raise HTTPException(
            status_code=413,
            detail=f"技能包超过上限 {MAX_ZIP_BYTES // 1024 // 1024}MB",
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        _audit("upload", file.filename, "failed", "不是合法的 zip")
        raise HTTPException(status_code=400, detail="不是合法的 zip 文件") from exc

    with archive:
        members = [m for m in archive.infolist() if not m.is_dir()]

        if not members:
            raise HTTPException(status_code=400, detail="技能包为空")
        if len(members) > MAX_ENTRIES:
            _audit("upload", file.filename, "blocked", f"条目数 {len(members)} 超限")
            raise HTTPException(status_code=413, detail=f"条目数超过上限 {MAX_ENTRIES}")

        # ---- 逐条目安全校验 ----
        for m in members:
            if reason := _is_unsafe_member(m.filename):
                _audit("upload", f"{file.filename}!{m.filename}", "blocked", reason)
                raise HTTPException(
                    status_code=403,
                    detail=f"技能包含不安全路径（{reason}）：{m.filename}",
                )
            # 符号链接可指向宿主任意位置，一律拒绝
            if (m.external_attr >> 16) & 0o170000 == 0o120000:
                _audit("upload", f"{file.filename}!{m.filename}", "blocked", "含符号链接")
                raise HTTPException(
                    status_code=403, detail=f"技能包含符号链接，已拒绝：{m.filename}"
                )

        total = sum(m.file_size for m in members)
        if total > MAX_TOTAL_BYTES:
            _audit("upload", file.filename, "blocked", f"解压后 {total} 字节超限")
            raise HTTPException(
                status_code=413,
                detail=f"解压后总大小超过上限 {MAX_TOTAL_BYTES // 1024 // 1024}MB",
            )

        # ---- 判定技能名与技能根目录 ----
        tops = {m.filename.replace("\\", "/").split("/")[0] for m in members}
        has_nested = any("/" in m.filename.replace("\\", "/") for m in members)

        if has_nested and len(tops) == 1:
            skill_name, strip_prefix = next(iter(tops)), True
        else:
            skill_name, strip_prefix = Path(file.filename).stem, False

        if not _NAME_RE.match(skill_name):
            _audit("upload", skill_name, "blocked", "技能名不合法")
            raise HTTPException(
                status_code=400,
                detail=f"技能名 {skill_name!r} 不合法（仅允许中英文、数字、下划线、连字符）",
            )

        target_dir = _skills_dir() / skill_name
        # 二次校验：最终目录必须真的在 skills/ 之内
        if not target_dir.resolve().is_relative_to(_skills_dir().resolve()):
            raise HTTPException(status_code=403, detail="技能目录越出 skills/")

        replaced = target_dir.is_dir()
        if replaced:
            shutil.rmtree(target_dir)

        # ---- 解压 ----
        written = 0
        try:
            for m in members:
                rel = m.filename.replace("\\", "/")
                if strip_prefix:
                    rel = rel.split("/", 1)[1] if "/" in rel else rel
                if not rel:
                    continue

                dest = target_dir / rel
                # 再次确认展开后的路径未逃逸
                if not dest.resolve().is_relative_to(target_dir.resolve()):
                    raise HTTPException(status_code=403, detail=f"路径越界：{m.filename}")

                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(m) as src, dest.open("wb") as out:
                    while chunk := src.read(64 * 1024):
                        written += len(chunk)
                        if written > MAX_TOTAL_BYTES:
                            raise HTTPException(status_code=413, detail="解压后超过大小上限")
                        out.write(chunk)
        except HTTPException:
            shutil.rmtree(target_dir, ignore_errors=True)
            _audit("upload", file.filename, "blocked", "解压过程被中止")
            raise
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(target_dir, ignore_errors=True)
            _audit("upload", file.filename, "failed", f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=500, detail=f"解压失败：{exc}") from exc

    if not (target_dir / "SKILL.md").is_file():
        shutil.rmtree(target_dir, ignore_errors=True)
        _audit("upload", file.filename, "failed", "缺少 SKILL.md")
        raise HTTPException(
            status_code=400,
            detail="技能根目录下未找到 SKILL.md，无法作为技能安装",
        )

    # ---- 重新生成快照，让 Agent 立即知道这个新技能 ----
    settings = get_settings()
    try:
        skills = scan_skills(
            settings.skills_path, settings.skills_snapshot_path, BACKEND_DIR.parent
        )
        snapshot_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.error("技能快照重建失败：%s", exc)
        snapshot_ok = False
        skills = []

    _audit("upload", f"{skill_name} ({written} 字节, {len(members)} 个文件)")
    logger.info("技能已安装：%s（%d 个文件，%d 字节）", skill_name, len(members), written)

    return {
        "name": skill_name,
        "files": len(members),
        "size": written,
        "replaced": replaced,
        "skills_total": len(skills),
        "snapshot_rebuilt": snapshot_ok,
        "message": f"技能 {skill_name} 已安装，技能快照已更新，"
        f"Agent 下一轮对话即可通过 read_file 读取其 SKILL.md。",
    }


@router.delete("/skills/{name}")
async def uninstall_skill(name: str) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"技能名不合法：{name!r}")

    target = _skills_dir() / name
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"技能不存在：{name}")
    if not target.resolve().is_relative_to(_skills_dir().resolve()):
        raise HTTPException(status_code=403, detail="路径越界")

    shutil.rmtree(target)

    settings = get_settings()
    scan_skills(settings.skills_path, settings.skills_snapshot_path, BACKEND_DIR.parent)

    _audit("uninstall", name)
    logger.info("技能已卸载：%s", name)
    return {"name": name, "deleted": True}
