#!/usr/bin/env bash
#
# code_test 技能的沙箱内执行封装。
#
# 为什么要有这个脚本：裸的 pytest 在本沙箱里需要一整套非默认参数才能跑通——
#   /workspace/src 只读          → 不能让 pytest 写 .pytest_cache 或 .pyc
#   /workspace 工作目录只读       → coverage 的数据文件必须显式改道到 /workspace/out
#   工具层对 stdout 有 5000 字符**从头截断** → pytest 的结论在末尾，会被整段切掉，
#                                 故完整日志写进归档文件，stdout 只留摘要
# 任何一处遗漏都会失败得难以定位，且模型每次复现长命令都可能出错。
#
# 用法（路径相对 /workspace/src/backend）：
#   airclaw-test <测试文件> <覆盖率目标>
#   例：airclaw-test tests/test_terminal_tool.py tools.terminal_tool
#       airclaw-test tests/test_terminal_tool.py tools/terminal_tool.py
#
# 产出（写入 /workspace/out，即宿主机的 reports/{模块}/{日期}/）：
#   pytest.log        完整执行日志
#   coverage.json     覆盖率原始数据
#   .coverage         coverage 数据文件
#   <测试文件名>      测试用例副本

set -uo pipefail

SRC=/workspace/src/backend
OUT=/workspace/out

die() { echo "[airclaw-test] 错误：$*" >&2; exit 2; }

[ $# -ge 2 ] || die "用法: airclaw-test <测试文件> <覆盖率目标>"
TEST="$1"
COV="$2"

cd "$SRC" || die "无法进入 $SRC"
[ -f "$TEST" ] || die "测试文件不存在：$SRC/$TEST"
mkdir -p "$OUT" || die "无法创建输出目录 $OUT"

# --cov 必须给**目录**，且不能给点分模块名。两条都是实测踩出来的：
#
#   给 `--cov=tools.read_file_tool`（点分名）→ coverage 会先 import 该模块来定位
#   源文件，触发 tools/__init__.py → knowledge_index → bm25s → numpy；随后 pytest
#   收集测试文件再导入一次，numpy 的 C 扩展被加载两遍，报
#     ImportError: cannot load module more than once per process
#   报错发生在收集阶段，看不出与 --cov 有关，极难定位。
#
#   给 `--cov=tools/read_file_tool.py`（文件路径）→ coverage 视为模块名，
#   报 "Module tools/read_file_tool.py was never imported"，覆盖率数据全空。
#
# 给目录 `--cov=tools` 两者都没有，且 coverage.json 里带逐文件明细，
# 摘要再从里面取被测文件自己的数字。
#
# 覆盖率目标的写法归一化：模型会写成 backend.graph.daily_log 或
# backend/graph/daily_log.py 这些形式，而工作目录已经是 backend/，
# 多一层前缀就定位不到文件——宽容处理，省一轮往返。
norm="${2#backend/}"
norm="${norm#backend.}"
norm="${norm%.py}"          # 也见点分带后缀的写法：graph.daily_log.py
COV_FILE=""
for cand in "$norm" "${norm}.py" "${norm//.//}.py" "${norm//.//}/__init__.py"; do
    if [ -f "$cand" ]; then COV_FILE="$cand"; break; fi
done

if [ -n "$COV_FILE" ]; then
    COV_SOURCE="$(dirname "$COV_FILE")"
else
    # 定位不到：退回把参数当目录用，让 coverage 报出来，摘要里会提示
    COV_SOURCE="${norm//.//}"
fi

cp "$TEST" "$OUT/" 2>/dev/null \
    || echo "[airclaw-test] 警告：测试用例副本未能写入归档目录" >&2

COVERAGE_FILE="$OUT/.coverage" \
python -m pytest "$TEST" \
    -v -p no:cacheprovider --rootdir="$SRC" \
    --cov="$COV_SOURCE" --cov-report=term-missing \
    --cov-report=json:"$OUT/coverage.json" \
    > "$OUT/pytest.log" 2>&1
STATUS=$?

python - "$OUT" "$TEST" "$COV" "$COV_FILE" "$STATUS" <<'PY'
import json
import re
import sys
from pathlib import Path

out, test, cov = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
cov_file, status = sys.argv[4], int(sys.argv[5])

log_file = out / "pytest.log"
log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""

print(f"[airclaw-test] 测试文件 {test}，覆盖率目标 {cov}，退出码 {status}"
      f"（0 为全部通过，1 为存在失败/错误，2 为收集出错）")

if not cov_file:
    print(f"[airclaw-test] 注意：无法把覆盖率目标 {cov} 对应到具体文件，统计按目录处理。"
          f"建议改传相对 backend/ 的导入名，例如 graph.daily_log 而不是 backend.graph.daily_log")

# pytest 的收尾行形如 "===== 2 failed, 3 passed in 0.12s ====="
banners = [ln.strip("= ") for ln in log.splitlines()
           if ln.startswith("=") and ln.rstrip().endswith("=")]
if banners:
    print(f"[airclaw-test] 结果：{banners[-1]}")

# 收集阶段就失败时，测试根本没跑起来，需明确指出
if "error during collection" in log or "errors during collection" in log:
    print("[airclaw-test] 收集阶段出错，测试未执行。原因见 pytest.log 的 ERRORS 段。")
    for line in log.splitlines():
        if line.startswith("E   ") or line.startswith("ERROR "):
            print(f"    {line.strip()}")

coverage_json = out / "coverage.json"
if coverage_json.is_file():
    data = json.loads(coverage_json.read_text(encoding="utf-8"))
    files = data.get("files", {})
    # 只引用被测文件自己的数字：--cov 给的是目录，totals 是整个包的合计，
    # 用它会把「本模块覆盖率」报成整包的，误导性极大。
    entry = files.get(cov_file) if cov_file else None
    if entry is None and cov_file:
        print(f"[airclaw-test] 覆盖率：coverage.json 中没有 {cov_file}，"
              f"已收录的是：{'、'.join(sorted(files))}")
    stats = (entry or {}).get("summary") or (data.get("totals") or {})
    branches = stats.get("num_branches") or 0
    branch = (f"{stats.get('covered_branches', 0) / branches:.1%}"
              if branches else "无分支")
    print(f"[airclaw-test] 覆盖率（{cov_file or '整包'}）："
          f"行 {stats.get('percent_covered_display', '?')}%，分支 {branch}，"
          f"未覆盖 {stats.get('missing_lines', '?')} 行")
else:
    print("[airclaw-test] 覆盖率：未生成 coverage.json，见 pytest.log 的 CoverageWarning")

# 失败用例明细：pytest 的 short test summary info 段
failures = re.findall(r"^(?:FAILED|ERROR) (\S+?)(?:\s+-\s+(.*))?$", log, re.MULTILINE)
if failures:
    print(f"[airclaw-test] 失败/错误用例 {len(failures)} 个（最多列 10 个）：")
    for name, reason in failures[:10]:
        print(f"    {name}" + (f"  —  {reason.strip()}" if reason else ""))

print(f"[airclaw-test] 归档目录 {out} 内容：" +
      "、".join(sorted(p.name for p in out.iterdir())))
print("[airclaw-test] 完整输出见归档目录的 pytest.log，"
      "用 read_file 读取对应宿主机路径即可")
PY
