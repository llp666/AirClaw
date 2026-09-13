"""审计日志核查报告。

**用途**：PRD 第八章第 4 条要求审计日志「以文件形式供定期人工核查」。日志是只追加的
JSONL，直接翻文件能看，但不方便——「哪些调用真的被拦了」这个问题，要在几百行里靠肉眼筛。
本脚本把日志读成一份**待复核清单**，按处置结果分组。

运行（在 `backend/` 下）：

    python graph/audit_report.py                    # 今天，待复核清单
    python graph/audit_report.py --date 2026-09-12
    python graph/audit_report.py --since 2026-09-01 --until 2026-09-13
    python graph/audit_report.py --session 18cc53bfd7b7
    python graph/audit_report.py --status blocked   # 只看规则阻断
    python graph/audit_report.py --tool terminal    # 只看某个工具
    python graph/audit_report.py --full             # 结果摘要不截断
    python graph/audit_report.py --all              # 不过滤，逐条列出全部记录

**只读**：不写入、不修改、不删除任何审计记录（PRD：日志禁止修改与删除）。本脚本也不
提供任何网络入口——PRD 明确「不提供在线查询接口与统计报表」，核查走文件与运维通道，
不为它开前端访问面。审计目录的读取权限由部署侧控制（见 audit_hooks.harden_audit_dir）。

**默认列出哪些**：blocked（规则阻断）、flagged（命中关注规则）、failed（执行失败）。
executed 与 deduplicated 不进清单，只在计数里出现。

**复核之后**：若认定是规则误伤，改 `rules/secrecy_rules.json` 后必须重跑
`python rules/check_rules.py` 回归——规则是纯数据，没有编译期错误可依赖。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

# 允许直接以 `python graph/audit_report.py` 运行（此时 sys.path[0] 是 graph/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings  # noqa: E402

#: 默认进清单的处置结果，元组顺序即报告里的分组顺序
REVIEW_GROUPS: tuple[tuple[str, str], ...] = (
    ("blocked", "必须复核：被审查规则阻断，该次调用未执行"),
    ("flagged", "建议复核：命中关注规则，已放行"),
    ("failed", "执行失败（多为环境或参数问题，非合规事件）"),
)
REVIEW_STATUSES = tuple(status for status, _ in REVIEW_GROUPS)

#: 结果摘要在清单里的显示长度。完整内容在 JSONL 里，用 --full 也能看全
RESULT_PREVIEW = 100

STATUS_LABEL = {
    "executed": "已执行",
    "blocked": "已阻断",
    "flagged": "已标记",
    "failed": "失败",
    "deduplicated": "去重跳过",
}


def _parse_date(text: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到：{text}")


def _parse_statuses(text: str) -> set[str] | None:
    """`all` 表示不按处置结果过滤。"""
    items = {s.strip() for s in text.split(",") if s.strip()}
    if not items or "all" in items:
        return None
    known = set(STATUS_LABEL)
    unknown = items - known
    if unknown:
        raise argparse.ArgumentTypeError(
            f"未知的处置结果 {sorted(unknown)}，可选：{sorted(known)} 或 all"
        )
    return items


def file_date(path: Path) -> date | None:
    try:
        return datetime.strptime(path.stem.removeprefix("audit_"), "%Y%m%d").date()
    except ValueError:
        return None


def _select_files(audit_dir: Path, since: date, until: date) -> list[Path]:
    return [
        path
        for path in sorted(audit_dir.glob("audit_*.jsonl"))
        if (day := file_date(path)) is not None and since <= day <= until
    ]


def load_entries(files: list[Path]) -> tuple[list[dict], int]:
    """读日志，返回 (记录, 损坏行数)。

    进程被杀时最后一行可能是半截 JSON。那是环境异常，不该让整份报告崩掉，但也不能
    当作没发生，故计数后在页脚印出来。
    """
    rows: list[dict] = []
    broken = 0
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                broken += 1
    return rows, broken


def _session_titles(sessions_dir: Path, ids: set[str]) -> dict[str, str]:
    """取会话标题，让报告里显示的是「18cc53bfd7b7『杀伤网建模方法综述』」。

    knowledge_api / 文件接口这类来源没有对应会话文件，查不到就只显示 id。
    """
    titles: dict[str, str] = {}
    for sid in ids:
        path = sessions_dir / f"{sid}.json"
        if not path.is_file():
            continue
        try:
            title = json.loads(path.read_text(encoding="utf-8")).get("title", "")
        except (json.JSONDecodeError, OSError):
            continue
        if title:
            titles[sid] = title
    return titles


def _keep(row: dict, statuses: set[str] | None, session: str | None, tool: str | None) -> bool:
    if statuses is not None and row.get("result_status") not in statuses:
        return False
    if session and row.get("session_id") != session:
        return False
    if tool and row.get("tool_name") != tool:
        return False
    return True


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _line(row: dict, titles: dict[str, str], *, full: bool) -> list[str]:
    """一条记录渲染成若干行。

    默认把「结果摘要」压到 RESULT_PREVIEW 字符：它是给事后追溯用的，动辄两三百字，
    整份清单会被它撑成一片文字墙。要看全的用 --full，或者直接翻 JSONL。
    """
    sid = row.get("session_id", "?")
    title = titles.get(sid)
    who = f"{sid}「{title}」" if title else sid

    out = [f" {row.get('timestamp', '')[11:19]}  {row.get('tool_name', '?'):<20} {who}"]
    if row.get("block_reason"):
        out.append(f"   原因：{row['block_reason']}")
    if row.get("duration_ms") is not None:
        out.append(f"   耗时：{row['duration_ms']} ms")
    out.append(f"   参数：{row.get('params_digest', '')}")
    if row.get("result_digest"):
        result = row["result_digest"] if full else _clip(row["result_digest"], RESULT_PREVIEW)
        out.append(f"   结果：{result}")
    return out


def report(args: argparse.Namespace) -> int:
    settings = get_settings()
    audit_dir = settings.audit_path

    since = args.since or args.date or date.today()
    until = args.until or args.date or since
    if until < since:
        print(f"日期范围反了：{since} ~ {until}", file=sys.stderr)
        return 2

    files = _select_files(audit_dir, since, until)
    rows, broken = load_entries(files)

    statuses = None if args.all else (args.status or set(REVIEW_STATUSES))
    selected = [r for r in rows if _keep(r, statuses, args.session, args.tool)]

    print(f"审计核查报告 · {since} ~ {until}")
    print(f"数据源：{audit_dir}（{', '.join(p.name for p in files) or '该范围内没有文件'}）")
    counts = Counter(r.get("result_status", "?") for r in rows)
    summary = " · ".join(f"{STATUS_LABEL.get(k, k)} {v}" for k, v in counts.most_common())
    print(f"记录 {len(rows)} 条：{summary or '（空）'}")

    scope = "全部记录" if args.all else "待复核记录"
    line = f"{scope} {len(selected)} 条"
    filters = [
        f"会话={args.session}" if args.session else "",
        f"工具={args.tool}" if args.tool else "",
        f"状态={','.join(sorted(args.status))}" if args.status else "",
    ]
    filters = [f for f in filters if f]
    if filters:
        line += "（筛选：" + "，".join(filters) + "）"
    print(line)
    print()

    if not files:
        print("该日期范围内没有审计文件。")
        return 0

    titles = _session_titles(
        settings.sessions_path, {r.get("session_id", "") for r in selected}
    )

    if args.all:
        for row in selected:
            print("\n".join(_line(row, titles, full=args.full)))
            print()
        return 0

    for status, heading in REVIEW_GROUPS:
        group = [r for r in selected if r.get("result_status") == status]
        print(f"── {heading}（{len(group)} 条）")
        if not group:
            print("   （无）")
            print()
            continue
        for row in group:
            print("\n".join(_line(row, titles, full=args.full)))
            print()

    if broken:
        print(f"注意：有 {broken} 行无法解析（写入中途被中断或文件损坏），未进本报告。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="审计日志核查报告（只读，不修改任何审计记录）",
        epilog="示例：python graph/audit_report.py --since 2026-09-01 --status blocked",
    )
    parser.add_argument("--date", type=_parse_date, help="单日，如 2026-09-13")
    parser.add_argument("--since", type=_parse_date, help="起始日期（含）")
    parser.add_argument("--until", type=_parse_date, help="结束日期（含）")
    parser.add_argument("--status", type=_parse_statuses, help="处置结果，逗号分隔，或 all")
    parser.add_argument("--session", help="只看某个会话 ID")
    parser.add_argument("--tool", help="只看某个工具名")
    parser.add_argument(
        "--all", action="store_true", help="不过滤，逐条列出范围内的全部记录"
    )
    parser.add_argument(
        "--full", action="store_true", help="结果摘要不截断（日志里最多 300 字符）"
    )
    return report(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
