"""审计日志保留与归档。

PRD 第八章第 4 条要求审计日志「只追加，禁止修改与删除历史记录」。所以本脚本的删除
**不会自动发生**：默认只演练（把将要做的事打印出来），必须显式加 `--apply` 才动文件。
什么时候跑、隔多久跑一次由部署方决定（Windows 任务计划 / cron），不由应用偷偷代劳。

规则：

    保留窗口    最近 N 天（默认 30）的分片文件原样保留，一个字节都不动。
    选择性归档  超期文件在删除前先把**重要条目**抽出来另存到
                audit/archive/YYYYMMDD.important.jsonl（默认 blocked / flagged 两类合规事件）。
                整份文件里没有重要条目时直接删除，不留空归档。
    留痕        归档与删除都会往当天审计日志追加一条记录（tool_name=audit_retention），
                所以「什么时候删了哪些」本身也在审计链里，可用
                `python graph/audit_report.py --tool audit_retention --status all` 查。

**被删掉的是什么**：超期文件里非重要条目（executed / deduplicated / failed）不进归档，
随原文件一起消失。若本单位的核查要求覆盖这些，用 `--keep-statuses` 把 failed 等一并纳入。

运行（在 `backend/` 下）：

    python graph/audit_retention.py                     # 演练，只打印，不动文件
    python graph/audit_retention.py --apply             # 执行归档与删除
    python graph/audit_retention.py --days 90
    python graph/audit_retention.py --keep-statuses blocked,flagged,failed

归档目录装的仍是日志内容，故与审计目录同样收权限（POSIX 下 0700）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# 允许直接以 `python graph/audit_retention.py` 运行（此时 sys.path[0] 是 graph/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings  # noqa: E402
from graph.audit_hooks import AuditLog, harden_audit_dir  # noqa: E402
from graph.audit_report import file_date, load_entries  # noqa: E402

ARCHIVE_DIRNAME = "archive"
DEFAULT_DAYS = 30

#: 默认归档哪些处置结果。「重要」指合规事件；executed / deduplicated / failed 是运行噪声
DEFAULT_KEEP = ("blocked", "flagged")

STATUS_CHOICES = ("executed", "blocked", "flagged", "failed", "deduplicated")


def _parse_statuses(text: str) -> tuple[str, ...]:
    items = tuple(s.strip() for s in text.split(",") if s.strip())
    if not items:
        raise argparse.ArgumentTypeError("至少要保留一类，否则归档会全是空的")
    unknown = set(items) - set(STATUS_CHOICES)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"未知的处置结果 {sorted(unknown)}，可选：{'、'.join(STATUS_CHOICES)}"
        )
    return items


def expired_files(audit_dir: Path, cutoff: date) -> list[Path]:
    """早于 cutoff 的按日分片文件，按日期升序。

    glob 是非递归的，故 audit/archive/ 里的归档不会被当成待处理对象。
    """
    found = [
        path
        for path in audit_dir.glob("audit_*.jsonl")
        if (day := file_date(path)) is not None and day < cutoff
    ]
    return sorted(found, key=lambda p: file_date(p) or date.min)


def run(args: argparse.Namespace) -> int:
    if args.days < 1:
        print("--days 必须 >= 1，否则会把今天的日志也判为超期", file=sys.stderr)
        return 2

    settings = get_settings()
    audit_dir = settings.audit_path
    archive_dir = audit_dir / ARCHIVE_DIRNAME
    cutoff = date.today() - timedelta(days=args.days)
    files = expired_files(audit_dir, cutoff)
    keep = set(args.keep_statuses)

    print(f"审计日志保留 · 保留最近 {args.days} 天（{cutoff} 及之后的文件不动）")
    print(f"审计目录：{audit_dir}")
    print(f"归档目录：{archive_dir}（归档 {'、'.join(args.keep_statuses)}）")
    print()

    if not files:
        total = len(list(audit_dir.glob("audit_*.jsonl")))
        print(f"没有超期文件：现存 {total} 个分片都在保留窗口内。")
        return 0

    # 先把计划算出来再动手：演练与执行走同一条路径，避免「演练说的」和「真做的」不一致
    plan: list[tuple[Path, list[dict], int, int]] = []
    for path in files:
        rows, broken = load_entries([path])
        important = [r for r in rows if r.get("result_status") in keep]
        plan.append((path, important, len(rows) - len(important), broken))

    total = sum(len(imp) + dropped for _, imp, dropped, _ in plan)
    print(f"超期文件 {len(plan)} 个，共 {total} 条记录：")
    for path, important, dropped, broken in plan:
        note = f"（另有 {broken} 行损坏，一并丢弃）" if broken else ""
        if important:
            print(f"  {path.name}  归档 {len(important)} 条，丢弃 {dropped} 条{note}")
        else:
            print(f"  {path.name}  无重要条目，整份删除（{dropped} 条）{note}")

    if not args.apply:
        print()
        print("以上为演练结果，未改动任何文件。确认无误后加 --apply 执行。")
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    harden_audit_dir(archive_dir)

    archived = deleted = 0
    oldest: date | None = None
    for path, important, _dropped, _broken in plan:
        day = file_date(path)
        try:
            if important:
                # 覆盖而非追加：某天的归档内容就该等于该天文件里的重要条目。
                # 上一轮若在归档之后、删除之前失败，追加会写出重复条目。
                target = archive_dir / f"{day:%Y%m%d}.important.jsonl"
                with target.open("w", encoding="utf-8") as fh:
                    for row in important:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                archived += len(important)
            path.unlink()
            deleted += 1
            oldest = day if oldest is None else min(oldest, day)
        except OSError as exc:
            print(
                f"\n处理 {path.name} 时失败，已停止（该文件尚未删除）：{exc}",
                file=sys.stderr,
            )
            return 1

    # 删除动作本身也要留痕，否则「日志为什么变少了」无从追溯
    AuditLog(audit_dir).write(
        session_id="ops",
        user=settings.app_user,
        tool_name="audit_retention",
        params_digest=(
            f"保留 {args.days} 天：归档 {archived} 条，删除 {deleted} 个分片"
            f"（最早 {oldest}）"
        ),
        result_status="executed",
    )

    print()
    print(f"完成：归档 {archived} 条 → {archive_dir}，删除 {deleted} 个分片文件。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="审计日志保留与归档（默认只演练，--apply 才执行）",
        epilog="示例：python graph/audit_retention.py --apply",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help=f"保留天数，默认 {DEFAULT_DAYS}"
    )
    parser.add_argument(
        "--keep-statuses",
        type=_parse_statuses,
        default=DEFAULT_KEEP,
        help=f"归档哪些处置结果，逗号分隔。默认 {'、'.join(DEFAULT_KEEP)}",
    )
    parser.add_argument(
        "--apply", action="store_true", help="真正执行归档与删除；不加则只演练"
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
