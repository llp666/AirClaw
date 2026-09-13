#!/bin/sh
#
# 容器启动引导。
#
# 职责：首次启动时把镜像内的应用代码灌入「代码卷」。
#
# 为什么需要这一步：
#   命名卷首次挂载时是**空**的。而应用与沙箱必须共用同一份代码卷
#   （应用读写它、沙箱只读它），所以代码必须真的落在卷里，
#   不能只存在于镜像层 —— 否则沙箱的 /workspace/src 是空的，
#   Agent 的 terminal 看不到任何文件，且不会报错。

set -e

SRC="${SANDBOX_SRC_MOUNT:-/srv/airclaw-src}"
OUT="${SANDBOX_OUT_MOUNT:-/srv/airclaw-out}"
SEED="/opt/airclaw"

if [ ! -f "${SRC}/backend/app.py" ]; then
    echo "[entrypoint] 代码卷为空，正在从镜像灌入初始代码到 ${SRC}"
    mkdir -p "${SRC}"
    cp -a "${SEED}/." "${SRC}/"
    echo "[entrypoint] 灌入完成"
else
    echo "[entrypoint] 代码卷已有内容，跳过灌入（保留卷内现有代码与数据）"
fi

# 运行期目录：命名卷首次挂载时不会自动建这些子目录
mkdir -p \
    "${SRC}/backend/sessions/archive" \
    "${SRC}/backend/audit" \
    "${SRC}/backend/reports" \
    "${SRC}/backend/knowledge" \
    "${SRC}/backend/storage/memory_index" \
    "${OUT}/tmp"

# 输出卷权限
#
# 沙箱容器以 uid 1000 运行，而本容器与卷都是 root 所有。若不放开，
# 沙箱内写入输出目录会报「Permission denied」——而且报错出现在被测脚本里，
# 不容易联想到这里。输出卷是单次执行的临时工作区，容器结束即销毁，放宽无副作用。
chmod 0777 "${OUT}" "${OUT}/tmp"

# tiktoken 词表：离线环境必须预置，否则首次统计 token 会尝试联网下载而失败。
# 卷内没有而镜像里有则补过去。
if [ -d "${SEED}/backend/storage/tiktoken_cache" ] && \
   [ ! -d "${SRC}/backend/storage/tiktoken_cache" ]; then
    mkdir -p "${SRC}/backend/storage/tiktoken_cache"
    cp -a "${SEED}/backend/storage/tiktoken_cache/." \
          "${SRC}/backend/storage/tiktoken_cache/"
    echo "[entrypoint] 已补入 tiktoken 词表缓存"
fi

echo "[entrypoint] 代码卷: ${SRC}"
echo "[entrypoint] 输出卷: ${OUT}"

exec "$@"
