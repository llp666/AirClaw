"""AirClaw 全局配置管理。

两类配置分开存放：

- **环境配置**：来自 `.env`（部署相关的地址、凭证、资源限额），通过 Settings 读取，只读。
- **运行配置**：来自 `config.json`（可由前端切换的开关，如 RAG 模式），可读写。

模型服务地址与密钥一律通过环境变量注入，不硬编码。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录，所有相对路径以此为基准
BACKEND_DIR = Path(__file__).resolve().parent

# 运行期可切换配置的持久化文件
CONFIG_JSON = BACKEND_DIR / "config.json"


class Settings(BaseSettings):
    """环境配置。字段名与 .env 中的变量名大小写不敏感地对应。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 内网私有化模型服务（Agent 主模型）----
    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_model: str = "deepseek-r1-distill-32b"
    llm_api_key: str = "changeme"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    llm_request_timeout: int = 120

    # 是否启用模型思考（Thinking）。
    # 仅对推理型模型（如本地原型用的 agnes-2.5-flash）有效，通过 chat_template_kwargs
    # 下发。实测开启时约半数以上 token 消耗在 reasoning 上，关闭后延迟约减半、正文更完整，
    # 但前端「思考链」将看不到模型推理过程，只剩工具调用链。
    # 内网部署的 DeepSeek-R1 蒸馏版等模型若不识别该参数，此项会被忽略，无副作用。
    llm_enable_thinking: bool = True

    # ---- 向量模型服务 ----
    # 本地原型：jina-embeddings-v5-omni-small（1024 维）
    # 内网部署：BGE-M3（1024 维）
    embedding_provider: str = "jina"
    embedding_base_url: str = "https://api.jina.ai/v1/embeddings"
    embedding_model: str = "jina-embeddings-v5-omni-small"
    embedding_api_key: str = "changeme"
    embedding_dimensions: int = 1024
    embedding_task: str = "retrieval.query"

    # ---- 服务监听 ----
    app_host: str = "0.0.0.0"
    app_port: int = 8002
    app_log_level: str = "INFO"

    # ---- 操作者身份 ----
    #
    # 部署级身份，写入审计日志的 user 字段，前端也在导航栏显示它。
    #
    # 本项目没有登录体系（PRD 未要求，只要求「显示当前登录用户，如 dev01」），
    # 因此这个值回答的是「这台部署是谁在用」，而不是「这次请求是谁发的」。
    # 单机多人共用一台部署时它不区分人——需要按人追溯时应由保密管理员改此配置，
    # 或在外层接入认证网关后另行改造。客户端传来的身份**不被信任**，一律以此为准。
    app_user: str = "dev01"

    # ---- 目录（相对 backend/）----
    memory_dir: str = "memory"
    sessions_dir: str = "sessions"
    skills_dir: str = "skills"
    knowledge_dir: str = "knowledge"
    # 待审语料目录。刻意放在 knowledge/ **之外**：知识库索引递归扫描 knowledge/，
    # 若待审文件在里面就得在索引逻辑里再加一层排除；分开放，
    # 「knowledge/ 里的就是已入库的」这条不变式天然成立。
    knowledge_pending_dir: str = "knowledge_pending"
    reports_dir: str = "reports"
    audit_dir: str = "audit"
    storage_dir: str = "storage"
    workspace_dir: str = "workspace"
    sandbox_dir: str = "sandbox"
    rules_dir: str = "rules"

    # ---- Docker 沙箱 ----
    sandbox_image: str = "airclaw-sandbox:latest"
    sandbox_timeout: int = 300
    sandbox_cpus: str = "2"
    sandbox_memory: str = "2g"
    sandbox_pids_limit: int = 256

    # ---- 沙箱挂载方式 ----
    #
    # 两种模式，由 sandbox_src_volume 是否为空决定：
    #
    #   绑定模式（默认，开发与宿主机直跑）
    #     直接把本地路径作为 bind 源。要求**容器内路径必须等于宿主机路径**，
    #     否则 Docker daemon 在宿主机上找不到该路径，沙箱会静默拿到空目录。
    #
    #   命名卷模式（容器化部署，推荐）
    #     改用 Docker 命名卷，由 Docker 自己解析，与宿主机路径无关，
    #     Windows / Linux 行为一致。应用容器与沙箱容器各自挂载同一批卷：
    #
    #       应用容器                        沙箱容器
    #       /srv/airclaw-src  ←─ airclaw-src ─→  /workspace/src  (只读)
    #       /srv/airclaw-out  ←─ airclaw-out ─→  /workspace/out  (读写)
    sandbox_src_volume: str = ""
    sandbox_out_volume: str = ""
    sandbox_src_mount: str = "/srv/airclaw-src"
    sandbox_out_mount: str = "/srv/airclaw-out"

    @property
    def sandbox_uses_volume(self) -> bool:
        return bool(self.sandbox_src_volume and self.sandbox_out_volume)

    # ---- 演示模式（把系统交给别人体验时用）----
    #
    # 打开后，api/demo_guard.py 会装上三道闸：
    #   共享口令   所有请求（含静态界面）要过一次 Basic 认证，拦掉「链接一传就全网可用」
    #   只读       除「发起对话」外的一切写操作被拒，访客删不掉会话/语料，也改不了系统提示词
    #   按 IP 限流 对话条数与单条长度都封顶，避免有人拿它刷你的模型额度
    # 另：演示模式下会话按访客 IP 隔离，访客互相看不到对方的对话。
    # 默认关闭，本机与内网正常使用完全不受影响。
    demo_mode: bool = False
    demo_password: str = ""
    demo_chat_per_hour: int = 30
    demo_max_message_chars: int = 2000

    # ---- 会话与上下文 ----
    prompt_component_max_chars: int = 20_000
    compress_ratio: float = 0.5

    # ---- 长期记忆检索（RAG 模式）----
    #
    # MEMORY.md 小于此字符数时**整篇注入** System Prompt，超过才改用检索片段注入。
    # 阈值的存在是因为检索本身会丢内容：按 256 字符切块取 top-k，一个几 KB 的文件
    # 会被切成若干块而只取回几块——实测曾经的 RAG 模式就是这样「开了反而丢记忆」。
    # 只有文件长到整篇注入的上下文成本超过检索收益时，检索才划算。
    memory_inline_max_chars: int = 4000

    # 检索模式下一轮注入的记忆片段数
    memory_retrieval_top_k: int = 3

    # ---- 检索相关性下限 ----
    #
    # 低于此余弦相似度的候选视为不相关，不再注入上下文；全部低于下限时返回空结果。
    #
    # 为什么要卡：语义检索在向量空间里**总能**找到「最近的邻居」，不加阈值就永远
    # 会返回 top-k，哪怕一条都不相关。而 RAG_GUIDANCE 告诉模型「若无相关片段，
    # 说明记忆中确实没有对应内容」——没有阈值时这句话是假的，模型会看到一堆
    # 不相干的片段被当作检索结果递过来，可能硬往当前话题上套。
    #
    # 为什么卡余弦而不是卡 RRF 融合分数：RRF 只吃排名不吃分数，一个完全不相干的
    # 查询在第一名上照样能拿到 1/(60+1)，它反映的是「谁更靠前」而非「是否相关」。
    # 余弦相似度有绝对含义，才适合做阈值。
    #
    # 0.35 是按原型所用 embedding 模型（jina-embeddings-v5，归一化）实测标定的：
    # 相关条目的正确项落在 0.56~0.73，不相关查询的最高分只有 0.24，中间有很宽的
    # 空档。**换成内网 BGE-M3 后需要用本单位语料重新标定**——不同模型的余弦
    # 分布不一样，这个值不能照搬。
    retrieval_min_score: float = 0.35

    # 单轮对话内的工具调用去重（幂等保护）。
    # 模型会重复发出完全相同的工具调用，对只读命令只是浪费，对有副作用的
    # 命令则会产生重复产物。关闭后所有调用都会真实执行。详见 graph/tool_dedup.py
    tool_dedup_enabled: bool = True

    # 单轮对话的图执行步数上限（一个工具调用算一步）。
    # LangGraph 默认 25，对 code_test 这类多步技能不够用：实测「读源码 → 写用例 →
    # 跑测试 → 读日志 → 改用例 → 重跑」这一串走下来就撞上限，报 GraphRecursionError
    # 并中断整轮。放宽到 60，仍保留一个兜底，避免模型真的陷入死循环时无限烧 token。
    agent_recursion_limit: int = 60

    @property
    def memory_path(self) -> Path:
        return BACKEND_DIR / self.memory_dir

    @property
    def sessions_path(self) -> Path:
        return BACKEND_DIR / self.sessions_dir

    @property
    def skills_path(self) -> Path:
        return BACKEND_DIR / self.skills_dir

    @property
    def knowledge_path(self) -> Path:
        return BACKEND_DIR / self.knowledge_dir

    @property
    def knowledge_pending_path(self) -> Path:
        return BACKEND_DIR / self.knowledge_pending_dir

    @property
    def reports_path(self) -> Path:
        return BACKEND_DIR / self.reports_dir

    @property
    def audit_path(self) -> Path:
        return BACKEND_DIR / self.audit_dir

    @property
    def storage_path(self) -> Path:
        return BACKEND_DIR / self.storage_dir

    @property
    def workspace_path(self) -> Path:
        return BACKEND_DIR / self.workspace_dir

    @property
    def sandbox_path(self) -> Path:
        return BACKEND_DIR / self.sandbox_dir

    @property
    def rules_path(self) -> Path:
        return BACKEND_DIR / self.rules_dir

    @property
    def skills_snapshot_path(self) -> Path:
        return BACKEND_DIR / "SKILLS_SNAPSHOT.md"

    @property
    def sandbox_tmp_path(self) -> Path:
        """沙箱临时工作区。

        命名卷模式下位于输出卷内（由 Docker 解析，不受宿主机路径影响）。
        绑定模式下**必须位于项目目录内**：Docker Desktop（Windows）默认只共享
        项目所在盘，挂载系统 %TEMP% 下的目录会触发「是否共享该目录」的确认，
        容器创建会挂起或直接失败（"user declined directory sharing"）。
        """
        if self.sandbox_uses_volume:
            return Path(self.sandbox_out_mount) / "tmp"
        return self.storage_path / "sandbox_tmp"

    @property
    def sandbox_src_path(self) -> Path:
        """沙箱可见的「被测代码目录」。

        命名卷模式下即代码卷的应用侧挂载点；绑定模式下为项目根目录。
        """
        if self.sandbox_uses_volume:
            return Path(self.sandbox_src_mount)
        return BACKEND_DIR.parent

    @property
    def tiktoken_cache_path(self) -> Path:
        """tiktoken BPE 词表缓存目录。

        默认落在系统临时目录，可能被清理；且本地部署无外网，首次使用某编码器时
        tiktoken 会尝试联网下载词表而失败。固定到项目内并预先下载，
        即可随部署包一起拷入内网。
        """
        return self.storage_path / "tiktoken_cache"

    @property
    def secrecy_rules_path(self) -> Path:
        return self.rules_path / "secrecy_rules.json"

    def memory_inline_ok(self) -> bool:
        """MEMORY.md 是否小到可以直接整篇注入。

        RAG 模式与 prompt_builder 都要据此决定「整篇注入还是检索」，
        故放在配置上作为唯一判据。
        """
        try:
            size = (self.memory_path / "MEMORY.md").stat().st_size
        except OSError:
            return True  # 文件不存在或读不到：没有记忆可注入，走注入分支无害
        return size <= self.memory_inline_max_chars

    def ensure_dirs(self) -> None:
        """创建运行期所需目录。幂等，启动时调用一次。"""
        for path in (
            self.memory_path / "logs",
            self.sessions_path / "archive",
            self.skills_path,
            self.knowledge_path,
            self.knowledge_pending_path,
            self.reports_path,
            self.audit_path,
            self.storage_path / "memory_index",
            self.sandbox_tmp_path,
            self.tiktoken_cache_path,
            self.workspace_path,
            self.rules_path,
        ):
            path.mkdir(parents=True, exist_ok=True)


class RuntimeConfig:
    """运行期可切换配置，持久化到 backend/config.json。

    与环境配置不同，这里的值可由前端通过 /api/config/* 接口修改。
    """

    def __init__(self, path: Path = CONFIG_JSON) -> None:
        self._path = path

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 配置损坏时回退到默认值，不阻断服务启动
            return {}

    def _write(self, data: dict) -> None:
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_rag_mode(self) -> bool:
        """RAG 模式开关。开启后 System Prompt 跳过 MEMORY.md，改为按需检索注入。"""
        return bool(self._read().get("rag_mode", False))

    def set_rag_mode(self, enabled: bool) -> None:
        data = self._read()
        data["rag_mode"] = bool(enabled)
        self._write(data)


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# 模块级单例
settings = Settings()
runtime_config = RuntimeConfig()

# tiktoken 在首次实例化某编码器时读取 TIKTOKEN_CACHE_DIR。必须在任何编码器
# 被创建之前设置，故置于模块加载期；目录同时建好，避免首次写入失败。
settings.tiktoken_cache_path.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(settings.tiktoken_cache_path))


def get_settings() -> Settings:
    return settings


def get_runtime_config() -> RuntimeConfig:
    return runtime_config


if __name__ == "__main__":
    # 便于人工核对当前生效的配置：python config.py
    s = get_settings()
    print(f"backend 目录      : {BACKEND_DIR}")
    print(f"LLM 服务          : {s.llm_base_url}  模型={s.llm_model}")
    print(f"Embedding 服务    : {s.embedding_base_url}  模型={s.embedding_model}")
    print(f"监听              : {s.app_host}:{s.app_port}")
    print(f"沙箱镜像          : {s.sandbox_image}  超时={s.sandbox_timeout}s")
    print(f".env 存在         : {os.path.exists(BACKEND_DIR / '.env')}")
    print(f"RAG 模式          : {get_runtime_config().get_rag_mode()}")
