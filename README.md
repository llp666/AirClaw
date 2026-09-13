<p align="center">
  <img src="image/logo.png" alt="AirClaw" width="120">
</p>

<h1 align="center">AirClaw</h1>

<p align="center">运行于本地的轻量级、可定制、可追溯、全透明 AI Agent 系统，面向科研生产一线的办公提效需求。</p>

<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.13-3776ab">
  <img alt="Next.js" src="https://img.shields.io/badge/Next.js-16-black">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-Compose-2496ed">
</p>

---

- **文件即记忆**：摒弃不透明的向量数据库，会话、记忆、审计全部以人类可读的 Markdown / JSON 落盘
- **技能即插件**：遵循 Anthropic Agent Skills 的指令遵循范式，技能是 Markdown 说明书而非 Python 函数
- **透明可控**：System Prompt 拼接、工具调用、记忆读写对开发者完全可见
- **数据不出网**：推理、Embedding、检索全部在内网完成；系统自身不具备任何互联网访问能力

## Docker 一键启动

```bash
# 1. 构建两个镜像
docker build -f docker/Dockerfile -t airclaw:latest .
docker build -f backend/sandbox/Dockerfile -t airclaw-sandbox:latest .

# 2. 配置模型服务地址与密钥
cd docker && cp ../backend/.env.example .env && vi .env

# 3. 启动
docker compose up -d
```

浏览器打开 `http://<本地IP>:8002` 即可。

## 功能

| 能力 | 说明 |
| --- | --- |
| 对话 | SSE 流式输出，思考链、工具调用链、检索结果分别可视化 |
| 技能 | Agent Skills 范式：技能是 `SKILL.md` 说明书，Agent 用 `read_file` 读取后自行组织执行。支持上传 `.zip` 技能包，拖入即用 |
| 知识库 | RAG 混合检索（中文 BM25 + 向量，RRF 融合），支持 PDF / Word / Markdown / txt / Excel / CSV |
| 长期记忆 | `MEMORY.md` 跨会话记忆；文件较小时整篇注入 System Prompt，变大后自动切换为按需检索 |
| 代码自动化测试 | 内置 `code_test` 技能：生成用例 → 沙箱执行 → 覆盖率 → 报告 → 归档 |
| 沙箱执行 | `terminal` / `python_repl` 的一切执行都在无网络 Docker 容器内完成，项目目录只读挂载；审计日志、凭证与规则库已从沙箱中遮蔽 |
| 审查与审计 | 工具调用前扫描参数、命中规则即阻断；全部调用写入按日分片的 JSONL 审计日志 |
| RAG 诊断 | `/admin` 独立页面，查看一次检索召回了哪些片段、各自分数与向量，便于调参 |

## 目录结构

```
AirClaw/
├── backend/
│   ├── app.py              # FastAPI 入口
│   ├── config.py           # 全局配置
│   ├── api/                # 路由层：chat / sessions / files / skills
│   │                       #   / knowledge / tokens / config / rag
│   ├── graph/              # Agent 引擎：agent / prompt_builder / session_manager
│   │                       #   / audit_hooks / tool_dedup / memory_indexer / daily_log
│   │                       #   / audit_report / audit_retention
│   ├── tools/              # 核心工具：terminal / python_repl / read_file
│   │                       #   / write_file / search_knowledge_base
│   ├── workspace/          # System Prompt 组件：SOUL / IDENTITY / USER / AGENTS
│   ├── skills/code_test/   # 内置技能
│   ├── rules/              # 审查规则库 + 回归自检脚本
│   ├── sandbox/            # 沙箱镜像与技能执行封装
│   ├── memory/             # MEMORY.md 长期记忆 + 每日日志
│   ├── sessions/           # 会话 JSON + 压缩归档
│   ├── knowledge/          # 知识库语料
│   ├── knowledge_pending/  # 待审语料
│   ├── reports/            # code_test 报告归档
│   └── audit/              # 审计日志 JSONL
├── frontend/               # Next.js + React + Tailwind
│   └── src/app/admin/      # RAG 检索诊断页
└── docker/                 # 应用镜像
```
