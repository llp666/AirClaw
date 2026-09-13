# AirClaw 应用镜像（命名卷模式）
#
# 设计要点：
#
#   镜像内的代码放在 /opt/airclaw，**作为「代码卷」的初始种子**。
#   容器启动时由 docker/entrypoint.sh 在首次运行（卷为空）时灌入代码卷，
#   之后应用与沙箱共用该卷：应用读写，沙箱只读。
#
#   为什么代码必须落在卷里：沙箱容器由 Docker daemon 创建，它只能看到
#   卷里的内容。若代码只存在于镜像层，沙箱的 /workspace/src 会是空的。
#   用命名卷而非绑定挂载，是因为绑定挂载的源由 daemon 在**宿主机**上解析，
#   容器内路径必须与宿主机路径完全一致；命名卷由 Docker 自行解析，无此约束。
#
# 构建（联网机或内网镜像源）：
#   docker build -f docker/Dockerfile -t airclaw:latest .

# ---------- 阶段 1：构建前端（Node 仅存在于构建期）----------
FROM node:22-alpine AS frontend-builder

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# 产出静态文件到 /build/out（next.config.ts 已设 output: 'export'）
RUN npm run build


# ---------- 阶段 2：运行时（不含 Node）----------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AIRCLAW_HOME=/srv/airclaw-src \
    SANDBOX_SRC_MOUNT=/srv/airclaw-src \
    SANDBOX_OUT_MOUNT=/srv/airclaw-out

# Python 依赖：单独一层，便于复用与离线构建缓存
#
# **用 requirements.lock.txt 而非 requirements.txt**：后者是版本区间，
# 每次构建可能解析出不同版本。实测踩过：用 requirements.txt 构建出的镜像里
# 是 llama-index-core 0.14.24，而开发期验证的是 0.12.52 —— 等于交付了一个
# 从未测试过的依赖组合。lock 文件是 pip freeze 产物，锁死实测通过的版本。
COPY backend/requirements.lock.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

# 应用代码：作为代码卷的初始种子。
# 前端静态产物一并放入种子目录，由 entrypoint 随代码灌入代码卷后，
# 后端即可从 <代码卷>/frontend/out 直接托管（无需 Node）。
COPY backend/ /opt/airclaw/backend/
COPY --from=frontend-builder /build/out /opt/airclaw/frontend/out

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 应用从代码卷运行；该目录在容器启动时由 entrypoint 灌入
WORKDIR /srv/airclaw-src/backend

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
