#!/usr/bin/env bash
#
# 在**联网机**上准备 AirClaw 的离线交付包。
#
# 产出 out/airclaw-delivery/ ，整体拷入内网后按 README 部署：
#
#   airclaw-delivery/
#   ├── dist/
#   │   ├── airclaw.tar            # 应用镜像
#   │   └── airclaw-sandbox.tar    # 沙箱镜像
#   ├── app/                       # 部署目录内容（代码 + 前端产物）
#   │   ├── backend/
#   │   └── frontend/out/
#   ├── docker-compose.yml
#   ├── .env.example
#   └── 部署说明.md
#
# 不含 knowledge/（敏感语料由管理员单独导入）、.env（凭证）、
# sessions/ audit/（运行期数据）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/out/airclaw-delivery"

echo "==> 项目根目录: ${ROOT}"
echo "==> 输出目录:   ${OUT}"

rm -rf "${OUT}"
mkdir -p "${OUT}/dist" "${OUT}/app"

# ---------------------------------------------------------------- 镜像

echo
echo "==> [1/4] 构建沙箱镜像（含被测代码依赖，镜像较大）"
docker build -f "${ROOT}/backend/sandbox/Dockerfile" -t airclaw-sandbox:latest "${ROOT}"

echo
echo "==> [2/4] 构建应用镜像（含前端静态导出，Node 仅用于构建期）"
docker build -f "${ROOT}/docker/Dockerfile" -t airclaw:latest "${ROOT}"

echo
echo "==> [3/4] 导出镜像为 tar"
docker save airclaw:latest         -o "${OUT}/dist/airclaw.tar"
docker save airclaw-sandbox:latest -o "${OUT}/dist/airclaw-sandbox.tar"

# ---------------------------------------------------------------- 部署目录

echo
echo "==> [4/4] 组装部署目录"

# 后端代码：排除本地环境、运行期数据与凭证
rsync -a --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '.env' \
  --exclude 'config.json' \
  --exclude 'sessions/*' \
  --exclude 'audit/*' \
  --exclude 'reports/*' \
  --exclude 'knowledge/*' \
  --exclude 'knowledge_pending/*' \
  --exclude 'storage/sandbox_tmp/' \
  "${ROOT}/backend/" "${OUT}/app/backend/"

# 保留空目录占位（.gitkeep 已在 exclude 之外）
mkdir -p "${OUT}/app/backend/sessions/archive" \
         "${OUT}/app/backend/audit" \
         "${OUT}/app/backend/reports" \
         "${OUT}/app/backend/knowledge" \
         "${OUT}/app/backend/knowledge_pending" \
         "${OUT}/app/backend/storage/memory_index" \
         "${OUT}/app/backend/storage/sandbox_tmp"

# tiktoken 词表：离线环境必须预置，否则首次统计 token 会尝试联网下载而失败
if [ -d "${ROOT}/backend/storage/tiktoken_cache" ]; then
  mkdir -p "${OUT}/app/backend/storage/tiktoken_cache"
  cp -r "${ROOT}/backend/storage/tiktoken_cache/." \
        "${OUT}/app/backend/storage/tiktoken_cache/"
  echo "    已包含 tiktoken 词表缓存"
else
  echo "    [警告] 未找到 tiktoken 词表缓存；离线环境需另行预置" >&2
fi

# 前端静态产物（部署目录里必须有，路径一致挂载会覆盖镜像内的那份）
mkdir -p "${OUT}/app/frontend"
if [ -d "${ROOT}/frontend/out" ]; then
  cp -r "${ROOT}/frontend/out" "${OUT}/app/frontend/out"
else
  echo "    [警告] 未找到 frontend/out；请先在 frontend/ 执行 npm run build" >&2
fi

# 部署文件
cp "${ROOT}/docker/docker-compose.yml" "${OUT}/"
cp "${ROOT}/backend/.env.example"      "${OUT}/.env.example"

# ---------------------------------------------------------------- 清单

echo
echo "==> 交付包内容"
du -sh "${OUT}" 2>/dev/null || true
find "${OUT}" -maxdepth 2 -type d | sed "s|${OUT}|  .|"

cat <<'EOF'

==> 完成。拷入内网后按以下步骤部署：

    # 1. 目标机：载入镜像
    docker load -i dist/airclaw.tar
    docker load -i dist/airclaw-sandbox.tar

    # 2. 放到部署目录
    mkdir -p /srv/airclaw && cd /srv/airclaw
    cp <交付包>/docker-compose.yml .
    cp <交付包>/.env.example .env

    # 3. 配置内网模型服务地址与密钥
    vi .env

    # 4. 启动（首次启动会把代码灌入代码卷，之后代码与数据都持久化在卷中）
    docker compose up -d

    # 5. 验证
    curl http://127.0.0.1:8002/api/health
    浏览器打开 http://<本机IP>:8002

  说明：
    - 首次启动 entrypoint 会从镜像把代码灌入 airclaw-src 卷。之后更新代码时，
      替换镜像并删掉该卷（docker volume rm airclaw-src）再启动即可重新灌入。
    - 会话、记忆、审计日志、知识库语料都在 airclaw-src 卷内，删卷会一并丢失。
    - 沙箱的临时工作区在 airclaw-out 卷内，可随时清空。

EOF
