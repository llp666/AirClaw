"""AirClaw 后端入口。

启动流程（lifespan）：
    1. 创建运行期目录
    2. scan_skills()            扫描 skills/**/SKILL.md，生成 SKILLS_SNAPSHOT.md
    3. agent_manager.initialize() 创建内网 LLM、注册工具、加载审计钩子
    4. memory_indexer.rebuild_index()  构建 MEMORY.md 向量索引（待实现）

随后注册 API 路由，全部挂载在 /api 前缀下。

运行：
    cd backend
    uvicorn app:app --port 8002 --host 0.0.0.0 --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import chat, config_api, files, knowledge, rag, sessions, skills, tokens
from config import BACKEND_DIR, get_runtime_config, get_settings
from graph.agent import agent_manager
from graph.audit_hooks import harden_audit_dir
from tools import scan_skills

logger = logging.getLogger("airclaw")

VERSION = "0.1.0"

# 前端静态导出产物（`cd frontend && npm run build` 生成）。
# 存在时由本服务直接托管，前后端同源，部署机不再需要 Node 运行时。
FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "out"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.app_log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings.ensure_dirs()
    # 审计目录只对属主开放（PRD 第八章 4）。接口层已排除 audit/，这里补文件系统层
    harden_audit_dir(settings.audit_path)
    logger.info("AirClaw %s 启动中", VERSION)
    logger.info("主模型      : %s @ %s", settings.llm_model, settings.llm_base_url)
    logger.info(
        "Embedding   : %s (%s 维) via %s",
        settings.embedding_model,
        settings.embedding_dimensions,
        settings.embedding_provider,
    )
    logger.info("监听        : %s:%s", settings.app_host, settings.app_port)
    logger.info("沙箱镜像    : %s (超时 %ss)", settings.sandbox_image, settings.sandbox_timeout)
    logger.info("RAG 模式    : %s", get_runtime_config().get_rag_mode())

    # 1. 技能扫描
    scan_skills(settings.skills_path, settings.skills_snapshot_path, BACKEND_DIR.parent)

    # 2. Agent 初始化（含工具注册与审查规则加载；沙箱镜像缺失会在此抛出）
    agent_manager.initialize(BACKEND_DIR.parent)

    # 3. MEMORY.md 检索索引（RAG 模式且记忆文件较大时使用）
    #    失败不阻断启动：索引内部会降级为纯 BM25，检索能力受限但服务可用。
    try:
        stats = agent_manager.rebuild_memory_index(force=False)
        logger.info(
            "记忆索引    : %d 个分块，向量=%s",
            stats.get("chunks", 0),
            "可用" if stats.get("vector_ok") else "不可用",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆索引构建失败，检索将降级：%s", exc)

    logger.info("初始化完成，API 文档见 /docs")
    yield
    logger.info("AirClaw 已停止")


app = FastAPI(
    title="AirClaw",
    version=VERSION,
    description="本地科研办公提效 Agent —— 文件即记忆，技能即插件，全程可审计。",
    lifespan=lifespan,
)

# 前端运行在 3000 端口。内网授权终端通过 http://<本机IP>:3000 访问，
# 因此不能限定具体来源主机，仅限定端口。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://[\w.\-]+:3000$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(sessions.router, prefix="/api", tags=["sessions"])
app.include_router(files.router, prefix="/api", tags=["files"])
app.include_router(skills.router, prefix="/api", tags=["skills"])
app.include_router(knowledge.router, prefix="/api", tags=["knowledge"])
app.include_router(tokens.router, prefix="/api", tags=["tokens"])
app.include_router(config_api.router, prefix="/api", tags=["config"])
app.include_router(rag.router, prefix="/api", tags=["rag"])

@app.get("/api/health", tags=["health"])
async def health() -> dict:
    """存活探针。回显关键配置，便于确认部署是否指向正确的内网服务。"""
    settings = get_settings()
    return {
        "status": "ok",
        "version": VERSION,
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
        "rag_mode": get_runtime_config().get_rag_mode(),
    }


# ---------------------------------------------------------------
# 前端静态产物挂载
#
# 必须放在**所有路由定义之后**：Starlette 按注册顺序匹配，而 Mount("/") 会
# 匹配任意路径。若它排在前面，/api/* 会先被静态挂载命中而返回 404 ——
# 实测踩过这个坑（/api/health 返回 404）。
#
# 注意 @app.get 装饰器形式的端点在文件底部，所以本段必须在它之后。
# ---------------------------------------------------------------
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    logger.info("已托管前端静态产物：%s", FRONTEND_DIST)
else:
    logger.info(
        "未找到前端构建产物（%s），仅提供 API。"
        "前端开发时请另行运行 `cd frontend && npm run dev`。",
        FRONTEND_DIST,
    )
